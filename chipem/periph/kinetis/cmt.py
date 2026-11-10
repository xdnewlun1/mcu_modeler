"""Kinetis CMT (carrier modulator transmitter, K22 RM ch. 41): carrier generator, modulator and the CMT_IRO pin.

Registers from the SVD (8-bit: CGH1, CGL1, CGH2, CGL2, OC, MSC, CMD1-4, PPS, DMA); behaviour hand-written.

* Clock (41.7.1): CMTCLK = bus clock / (PPS.PPSDIV + 1) / 2^MSC.CMTDIV (the "intermediate frequency" IF divided by
  the secondary prescaler).
* Carrier (41.7.2): high for CGHx and low for CGLx CMTCLK periods (primary CGH1/CGL1, secondary CGH2/CGL2 in FSK),
  free running from MCGEN, starting high; zero counts do not work on silicon (one `warn`, treated as 1).
* Modulator (41.7.3): a modulation cycle is a mark (gate open) then a space (gate closed).  Time and baseband modes:
  tmark = (CMD1:CMD2 + 1) x 8 / CMTCLK, tspace = CMD3:CMD4 x 8 / CMTCLK; FSK: tmark = (CMD1:CMD2 + 1) carrier
  periods, tspace = CMD3:CMD4 carrier periods and the carrier switches between the primary and secondary counts at
  the end of every cycle (phase coherent: a cycle starts with a carrier period).  In time mode a mark does not end,
  and a space does not end, while the carrier is high: both are extended to the next carrier falling edge (41.7.3).
  CMD1-4 are loaded when MCGEN is set and when each following cycle starts (double buffered).  EXSPC (41.7.4):
  cycles starting while it is set are all space (mark + space long).  Baseband (BASE): the carrier is held high,
  so IRO is high for the whole mark.
* EOCF (MSC bit 7) is set when the first cycle starts (MCGEN 0 -> 1) and at every cycle end (counter reload); it is
  cleared by reading MSC and then accessing CMD2 or CMD4, or by a DMA transfer.  Table 41-23: EOCF & EOCIE requests
  the "CMT" interrupt when DMA.DMA = 0, or the DMA (DMAMUX source 47) when DMA.DMA = 1 (the DMA acknowledge clears
  EOCF).  Clearing MCGEN lets the current cycle finish (MSC.MCGEN reads 0 at once), then the modulator stops.
* CMT_IRO (41.5.1): with OC.IROPEN = 0 the pin is not driven (high impedance: the PORT shows an input); with IROPEN
  = 1 it carries the modulator output while MCGEN runs, else the OC.IROL latch; CMTPOL = 0 makes it active low
  (inverted).  The transmission starts PPSDIV + 2 (CMTDIV = 0) or 2 x PPSDIV + 3 bus clocks after MCGEN is set
  (Table 41-5).  Carrier edges are driven one by one (PinBoard.drive_signal) only while a mark is on a muxed and
  enabled IRO pin; the stored log keeps the dense carrier as `wave` summaries (chipem/log.py).  Otherwise only
  cycle boundaries are events.  IROL reads back the latch.  SIM_SOPT2[PTD7PAD] (double drive) is not modelled.
* Every modulation cycle is recorded in `cycles` (start_ns, mark_end_ns, end_ns, kind) for tests and tools.
Not modelled: Stop/low-power-stop behaviour, the undefined reset values of the data registers (they reset to 0).
"""
from __future__ import annotations

from collections import deque
from typing import Any

from ..registry import register
from ._util import GatedBlock, build_regs, find_model, irq_number
from .clockutil import bus_hz
from .dma import dma_request

MCGEN, EOCIE, FSK, BASE, EXSPC, EOCF = 1, 2, 4, 8, 0x10, 0x80
IROL, CMTPOL, IROPEN = 0x80, 0x40, 0x20
SIG = "CMT_IRO"


@register("kinetis.cmt")
class CMT(GatedBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.gate = "CMT"
        ov = {"OC": {"ro_mask": 0x1F, "on_write": self._oc_write},
              "MSC": {"ro_mask": EOCF, "on_read": self._msc_read, "on_write": self._msc_write},
              "CMD2": {"on_read": self._cmd_access, "on_write": self._cmd_write},
              "CMD4": {"on_read": self._cmd_access, "on_write": self._cmd_write},
              "PPS": {"ro_mask": 0xF0}, "DMA": {"ro_mask": 0xFE, "on_write": lambda b, r, o, n: self._update()}}
        r = build_regs((name, "CMT"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.dma_source = int(params.get("dma_source", 47))
        self.cycles: deque[tuple[float, float, float, str]] = deque(maxlen=100_000)
        self._irqn: int | None = None
        self._line = False
        self._warned: set[str] = set()
        self._init_state()

    def _init_state(self) -> None:
        self.running = False            # modulator engine active
        self._stopping = False          # MCGEN cleared: finish the current cycle
        self._ev = None                 # next modulator boundary
        self._cev = None                # next carrier edge (pin output during a mark)
        self._msc_read_seen = False
        self._lvl: str | None = None    # level driven on CMT_IRO (None = not driven)
        self._active = False            # modulator output (before polarity)
        self._secondary = False         # FSK: secondary carrier set in use
        self._c0 = 0.0                  # carrier phase origin (ns)
        self._th = self._tl = 0.0       # carrier high / low time (ns)
        self._mark_end = 0.0
        self._cycle_end = 0.0
        self._in_mark = False
        self._baseband = False

    # ------------------------------------------------------------------ setup
    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irqn = irq_number(chip, "CMT", self.name)
        mux = find_model(chip, "kinetis.dmamux")
        if mux is not None:
            mux.register_source(self.dma_source, self._dma_active)
            if hasattr(mux, "register_ack"):
                mux.register_ack(self.dma_source, self._dma_done)
        for b in chip.peripherals.values():
            if getattr(b, "model", None) == "kinetis.port":
                hooks = getattr(b, "signal_dir_hooks", None)
                if hooks is not None and self._pin_dir not in hooks:
                    hooks.append(self._pin_dir)

    def reset(self) -> None:
        chip = self.chip
        if chip is not None:
            chip.sched.cancel(getattr(self, "_ev", None))
            chip.sched.cancel(getattr(self, "_cev", None))
            if getattr(self, "_line", False) and self._irqn is not None:
                self.irq(self._irqn, False)
        self._line = False
        had = getattr(self, "_lvl", None) is not None
        self._warned = set()
        self._init_state()
        super().reset()
        if had and chip is not None and chip.pins is not None:
            chip.pins.release_signal(SIG)
            self._refresh_ports()

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=msg)

    # ------------------------------------------------------------------ clocks
    def cmtclk_hz(self) -> float:
        if self.chip is None:
            return 0.0
        msc = self.get("MSC")
        return bus_hz(self.chip) / ((self.get("PPS") & 0xF) + 1) / (1 << ((msc >> 5) & 3))

    def carrier_counts(self, secondary: bool = False) -> tuple[int, int]:
        h, lo = (self.get("CGH2"), self.get("CGL2")) if secondary else (self.get("CGH1"), self.get("CGL1"))
        if (h == 0 or lo == 0) and not self.get("MSC") & BASE:
            self._warn_once("zero", "CMT: a carrier high/low count is 0 (the carrier generator does not work on "
                            "silicon): treated as 1")
        return max(h, 1), max(lo, 1)

    def carrier_hz(self, secondary: bool = False) -> float:
        h, lo = self.carrier_counts(secondary)
        return self.cmtclk_hz() / (h + lo)

    def _start_delay_ns(self) -> float:
        pps = self.get("PPS") & 0xF
        cyc = pps + 2 if not (self.get("MSC") >> 5) & 3 else 2 * pps + 3
        return cyc * 1e9 / bus_hz(self.chip)

    # ------------------------------------------------------------------ pin
    def _pin_live(self) -> bool:
        pins = getattr(self.chip, "pins", None)
        if pins is None or not self.get("OC") & IROPEN:
            return False
        return any(pins.signal_matches(p.owner, SIG) for p in pins.signal_pins(SIG))

    def _pin_dir(self, owner: str) -> tuple[str, Any] | None:
        if "CMT_IRO" not in owner:
            return None
        if not self.get("OC") & IROPEN:
            return "in", None                                     # high impedance
        return "out", self._lvl or self._level_for(self._out_active())

    def _refresh_ports(self) -> None:
        chip = self.chip
        if chip is None or chip.pins is None:
            return
        for b in chip.peripherals.values():
            f = getattr(b, "refresh_function", None)
            if f is not None and getattr(b, "model", None) == "kinetis.port":
                f(lambda owner: "CMT_IRO" in str(owner))

    def _level_for(self, active: bool) -> str:
        pol = bool(self.get("OC") & CMTPOL)
        return "H" if active == pol else "L"

    def _out_active(self) -> bool:
        if self.running:
            return self._active
        return bool(self.get("OC") & IROL)

    def _apply(self) -> None:
        """Drive CMT_IRO from the current modulator output / latch."""
        if self.chip is None:
            return
        if not self.get("OC") & IROPEN:
            if self._lvl is not None:
                self._lvl = None
                if self.chip.pins is not None:
                    self.chip.pins.release_signal(SIG)
            return
        lvl = self._level_for(self._out_active())
        if lvl != self._lvl:
            self._lvl = lvl
            pins = self.chip.pins
            if pins is not None and pins.signal_pins(SIG):
                pins.drive_signal(SIG, lvl)

    def iro_level(self) -> str | None:
        return self._lvl

    # ------------------------------------------------------------------ modulator engine
    def _carrier_set(self, t: float) -> None:
        h, lo = self.carrier_counts(self._secondary)
        tc = 1e9 / self.cmtclk_hz()
        self._th, self._tl = h * tc, lo * tc

    def _carrier_high(self, t: float, at_edge: bool = True) -> bool:
        """Carrier level at t; at a rising edge instant it counts as high when `at_edge` (a mark starting there
        outputs the pulse) and as low otherwise (a mark ending there does not cut a pulse)."""
        p = self._th + self._tl
        ph = (t - self._c0) % p
        if ph < 1e-6 or p - ph < 1e-6:
            return at_edge
        return ph < self._th - 1e-9

    def _next_fall(self, t: float) -> float:
        """First carrier falling edge at or after t."""
        p = self._th + self._tl
        k = (t - self._c0) // p
        f = self._c0 + k * p + self._th
        return f if f >= t - 1e-9 else f + p

    def _start(self) -> None:
        chip = self.chip
        self.running = True
        self._stopping = False
        self._secondary = False
        t = chip.now_ns() + self._start_delay_ns()
        self._c0 = t
        self._carrier_set(t)
        first = self._cmd_values()                             # MCGEN initialises the modulator counters
        self.set("MSC", self.get("MSC") | EOCF)               # initial transmission begins
        self._update()
        self._ev = chip.sched.at_ns(t, self._cycle_start, t, first)

    def _cmd_values(self) -> tuple[int, int]:
        return (self.get("CMD1") << 8) | self.get("CMD2"), (self.get("CMD3") << 8) | self.get("CMD4")

    def _cycle_start(self, t: float, vals: tuple[int, int] | None = None) -> None:
        self._ev = None
        chip = self.chip
        if self._stopping:
            self._finish()
            return
        msc = self.get("MSC")
        fsk = bool(msc & FSK) and not msc & BASE
        self._baseband = bool(msc & BASE)
        mb, sb = vals if vals is not None else self._cmd_values()
        if fsk:
            self._c0 = t                                       # a cycle starts with a full carrier period
            self._carrier_set(t)
            p = self._th + self._tl
            mark_end = t + (mb + 1) * p
            end = mark_end + sb * p
        else:
            tick = 8e9 / self.cmtclk_hz()
            mark_end = t + (mb + 1) * tick
            if not self._baseband and self._carrier_high(mark_end, False):
                mark_end = self._next_fall(mark_end)            # the mark does not cut a carrier pulse
            end = mark_end + sb * tick
            if sb and not self._baseband and self._carrier_high(end, False):
                end = self._next_fall(end)
        kind = "space" if msc & EXSPC else ("fsk2" if fsk and self._secondary else ("fsk" if fsk else
                                            ("baseband" if self._baseband else "time")))
        if msc & EXSPC:
            mark_end = t                                       # extended space: no mark
        self.cycles.append((t, mark_end, end, kind))
        self._mark_end, self._cycle_end = mark_end, end
        self._in_mark = mark_end > t
        if self._in_mark:
            self._set_active(True if self._baseband else self._carrier_high(t), t)
            if not self._baseband and self._pin_live():
                self._sched_carrier(t)
        else:
            self._set_active(False, t)
        if self._in_mark:
            self._ev = chip.sched.at_ns(mark_end, self._mark_done)
        else:
            self._ev = chip.sched.at_ns(end, self._cycle_end_ev)

    def _sched_carrier(self, t: float) -> None:
        p = self._th + self._tl
        ph = (t - self._c0) % p
        nxt = t + (self._th - ph if ph < self._th - 1e-9 else p - ph)
        if nxt < self._mark_end - 1e-9:
            self._cev = self.chip.sched.at_ns(nxt, self._carrier_edge, nxt)

    def _carrier_edge(self, t: float) -> None:
        self._cev = None
        if not self._in_mark or not self.running:
            return
        self._set_active(self._carrier_high(t + 1e-6), t)
        self._sched_carrier(t)

    def _set_active(self, on: bool, t: float) -> None:
        self._active = on
        self._apply()

    def _mark_done(self) -> None:
        self._ev = None
        self.chip.sched.cancel(self._cev)
        self._cev = None
        self._in_mark = False
        self._set_active(False, self._mark_end)
        self._ev = self.chip.sched.at_ns(self._cycle_end, self._cycle_end_ev)

    def _cycle_end_ev(self) -> None:
        self._ev = None
        t = self._cycle_end
        msc = self.get("MSC")
        if msc & FSK and not msc & BASE:
            self._secondary = not self._secondary
        self.set("MSC", self.get("MSC") | EOCF)                # end of cycle: counter reload
        self._msc_read_seen = False
        self._update()
        self._cycle_start(t)

    def _finish(self) -> None:
        self.running = False
        self._stopping = False
        self._in_mark = False
        self._active = False
        self._apply()                                          # IRO back to the IROL latch

    # ------------------------------------------------------------------ IRQ / DMA
    def _dma_active(self) -> bool:
        msc = self.get("MSC")
        return bool(msc & EOCF and msc & EOCIE and self.get("DMA") & 1)

    def _dma_done(self) -> None:
        self.set("MSC", self.get("MSC") & ~EOCF)
        self._update()

    def _update(self) -> None:
        if self.chip is None:
            return
        msc = self.get("MSC")
        line = bool(msc & EOCF and msc & EOCIE and not self.get("DMA") & 1)
        if line != self._line and self._irqn is not None:
            self._line = line
            self.irq(self._irqn, line)
        if self._dma_active():
            dma_request(self.chip, self.dma_source)

    # ------------------------------------------------------------------ register hooks
    def _msc_read(self, blk, reg):
        if self.get("MSC") & EOCF:
            self._msc_read_seen = True
        return None

    def _cmd_access(self, blk, reg, *_):
        if self._msc_read_seen and self.get("MSC") & EOCF:
            self.set("MSC", self.get("MSC") & ~EOCF)            # read MSC, then access CMD2/CMD4
            self._msc_read_seen = False
            self._update()
        return None

    def _cmd_write(self, blk, reg, old, new):
        self._cmd_access(blk, reg)
        return None

    def _msc_write(self, blk, reg, old, new):
        if new & MCGEN and not old & MCGEN:
            if self.running and self._stopping:
                self._stopping = False                         # re-enabled before the last cycle ended
            elif not self.running:
                self._store(reg, new)
                self._start()
        elif old & MCGEN and not new & MCGEN and self.running:
            self._stopping = True                              # the current cycle completes first
        self._update()
        return None

    def _oc_write(self, blk, reg, old, new):
        if (old ^ new) & IROPEN:
            self._refresh_ports()
        if self._in_mark and not self._baseband and self._pin_live() and self._cev is None:
            self._sched_carrier(self.chip.now_ns())
        self._apply()
        return None
