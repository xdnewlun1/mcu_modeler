"""Piccolo (TMS320F2802x / F2806x) clocking, missing-clock logic and NMI watchdog.

`ti_c2000.piccolo_sysctrl` (0x7010-0x702F) extends the 2833x/280x `ti_c2000.sysctrl` model (sysctrl.py: watchdog
key/WDCHK/WDFLAG logic, LPMCR0 IDLE/STANDBY/HALT, PCLKCRx gates, PLL relock with the CPU in bypass) with the Piccolo
clock tree, from SPRUI09A (2802x TRM) 1.3 and SPRUH18I (2806x TRM) 1.3; register addresses SPRS523P Table 9-12,
SPRS698J Table 8-15:

* Clock sources (SPRUI09 Figure 1-18, Table 1-21 CLKCTL): INTOSC1 and INTOSC2 (10 MHz zero-pin oscillators,
  `params.intosc1_hz` / `intosc2_hz`), the crystal oscillator on X1/X2 (`params.xtal_hz`, 0 = no crystal fitted) and
  XCLKIN on GPIO38 or GPIO19 (XCLK.XCLKINSEL; `params.xclkin_hz`, 0 = nothing driven).  OSCCLK = INTOSC1 when
  CLKCTL.OSCCLKSRCSEL = 0 (reset), else OSCCLKSRC2 = INTOSC2 (OSCCLKSRC2SEL = 1) or the external clock (XTAL unless
  XTALOSCOFF, XCLKIN unless XCLKINOFF).  A write that changes OSCCLKSRCSEL forces PLLCR to 0 (PLL bypass), as on
  silicon ("prevents potential PLL overshoot").  INTOSCnOFF stops an oscillator.
* SYSCLKOUT (Table 1-23): OSCCLK x PLLCR.DIV (bypass when 0) / {4, 4, 2, 1}[PLLSTS.DIVSEL] -- DIVSEL = 3 (/1) is
  allowed with the PLL on, unlike the 2833x.  A PLLCR write relocks the PLL for PLLLOCKPRD OSCCLK cycles (reset
  0xFFFF; SPRS523 8.13.1.4: at least 10000 with the 10 MHz internal oscillators) with the CPU in bypass; PLLLOCKS
  reads 0 meanwhile.  DIV > 12 is reserved (2802x; 2806x up to 18, `params.max_div`).  The boot ROM leaves DIVSEL = 3
  (SPRUI09 2.2.3: "PLLSTS[DIVSEL] is set to 3 for SYSCLKOUT = CLKIN/1"), so `params.boot_divsel` defaults to 3:
  after a reset the CPU runs at INTOSC1 = 10 MHz, as firmware entered from the boot ROM sees it.
* CPU watchdog clock (Figure 1-37, CLKCTL.WDCLKSRCSEL): INTOSC1 (reset) or OSCCLKSRC2, / 512 / prescaler.  It keeps
  counting in HALT only with CLKCTL.WDHALTI = 1, and stops while its source is missing.  The watchdog reset drives
  XRS (resets PLLCR/PLLSTS/NMIFLG).
* Missing-clock detection (1.3.2.5, cases A-D): when OSCCLK disappears (selected source off or absent,
  `stop_oscillator()` for the external clock, PLLSTS.OSCOFF) and MCLKOFF = 0, after `params.mclk_detect_ns` (default
  the 13-bit VCOCLK counter at the limp frequency) CLOCKFAIL is raised (NMIFLG.CLOCKFAIL, the NMI with
  NMICFG.CLOCKFAIL, ePWM TZ5) and PLLSTS.MCLKSTS is set.  CLKCTL.NMIRESETSEL = 0: a missing-clock reset (MCLKRS)
  follows at once (cases A/C; PLLCR/PLLSTS/CLKCTL/NMIFLG kept, the CPU continues in limp mode).  NMIRESETSEL = 1:
  OSCCLKSRC2 failing switches OSCCLK back to INTOSC1, zeroes PLLCR and clears MCLKSTS by itself (case D); INTOSC1
  failing leaves the CPU in limp mode (case B); the NMI watchdog (`ti_c2000.nmiwd`) resets after NMIWDPRD SYSCLKOUT
  unless the NMI handler clears the flags.  WDCLKSRCSEL is forced to 0 on a detected failure.
Not modelled: oscillator trims (INTOSCnTRIM are storage; the internal oscillators run at their nominal frequency),
INTOSC drift, VREG/NORMRDY gating (PLLSTS.NORMRDYE), XCLKOUT on a pin, CPU-timer 2 clock sources other than
SYSCLKOUT (CLKCTL.TMR2CLKSRCSEL / TMR2CLKPRESCALE: one `warn`), JTAGDEBUG (storage).

`ti_c2000.nmiwd` (0x7060, SPRUI09 1.3.2.7, Table 1-26): NMICFG.CLOCKFAIL (write-1-only enable), NMIFLG
(CLOCKFAIL, NMIINT; reset by XRS only), NMIFLGCLR (write 1 to clear), NMIFLGFRC.CLOCKFAIL (force, for tests),
NMIWDCNT (counts SYSCLKOUT while an enabled fail flag is set; read-only), NMIWDPRD (reset 0xFFFF).  An enabled
CLOCKFAIL latches NMIFLG.NMIINT and pulses the CPU NMI (no further NMI until NMIINT is cleared).  The counter reaching
NMIWDPRD fires NMIRS, which resets the device through MCLKRS (internal; XRS stays high).  Writing NMIWDPRD equal to
the running count fires NMIRS at once; a period below the count lets the counter run to 0xFFFF and wrap first.
"""
from __future__ import annotations

from typing import Any

from ..registry import register
from ._util import C2000Block, build_regs, find_model, fmt_hz
from .sysctrl import MCLKCLR, MCLKOFF, MCLKSTS, OSCOFF, SysCtrl

# CLKCTL (SPRUI09 Table 1-21)
OSCCLKSRCSEL, OSCCLKSRC2SEL, WDCLKSRCSEL = 1 << 0, 1 << 1, 1 << 2
INTOSC1OFF, INTOSC1HALTI, INTOSC2OFF, INTOSC2HALTI = 1 << 8, 1 << 9, 1 << 10, 1 << 11
WDHALTI, XCLKINOFF, XTALOSCOFF, NMIRESETSEL = 1 << 12, 1 << 13, 1 << 14, 1 << 15
XCLKINSEL = 1 << 6                       # XCLK: 1 = GPIO19, 0 = GPIO38
_DIVSEL_DIV = (4, 4, 2, 1)
VCOCLK_COUNTER = 1 << 13                 # 13-bit VCOCLK counter (Figure 1-27): overflows without OSCCLK


@register("ti_c2000.piccolo_sysctrl")
class PiccoloSysCtrl(SysCtrl):
    implements = ("ti_c2000.sysctrl",)

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        params.setdefault("boot_divsel", 3)
        self.intosc1_hz = float(params.get("intosc1_hz", 10_000_000))
        self.intosc2_hz = float(params.get("intosc2_hz", 10_000_000))
        self.xtal_hz = float(params.get("xtal_hz", 0) or 0)
        self.xclkin_hz = float(params.get("xclkin_hz", 0) or 0)
        self.max_div = int(params.get("max_div", 12))     # 2802x PLLCR.DIV 3:0, 0-12 (SPRUI09 Table 1-23);
        self.div_mask = 0x1F if self.max_div > 15 else 0xF   # 2806x DIV 4:0, 0-18 (SPRUH18 Figure 1-25, Table 1-24)
        self.ext_ok = True                       # external clock source healthy (stop_oscillator / start_oscillator)
        self.intosc_ok = {1: True, 2: True}      # internal oscillators healthy (fail_intosc tests)
        super().__init__(name, base, size, regs, params=params, log=log)
        for reg, cb in (("CLKCTL", self._clkctl_write), ("XCLK", self._xclk_write)):
            if reg in self.regs:
                self.regs[reg].on_write = cb
        self.clockfails = 0
        self._tmr2_warned = False

    # the base model keeps OSCCLK in `ext_hz`; on the Piccolo it is the currently selected source
    @property
    def ext_hz(self) -> float:            # type: ignore[override]
        return self.oscclk_hz()

    @ext_hz.setter
    def ext_hz(self, v: float) -> None:
        pass                                   # SysCtrl.__init__ assigns params.ext_hz: the Piccolo ignores it

    # ------------------------------------------------------------------ clock sources
    def _clkctl(self) -> int:
        return self.get("CLKCTL") if "CLKCTL" in self.regs else 0

    def ext_source(self) -> tuple[str, float]:
        """(name, Hz) of the external clock OSCCLKSRC2 uses when OSCCLKSRC2SEL = 0: the crystal oscillator or XCLKIN."""
        c = self._clkctl()
        if not c & XTALOSCOFF and self.xtal_hz:
            return "XTAL", self.xtal_hz
        if not c & XCLKINOFF and self.xclkin_hz:
            pin = "GPIO19" if self.get("XCLK") & XCLKINSEL else "GPIO38"
            return f"XCLKIN ({pin})", self.xclkin_hz
        return "external", self.xtal_hz or self.xclkin_hz or 0.0

    def source(self) -> str:
        c = self._clkctl()
        if not c & OSCCLKSRCSEL:
            return "INTOSC1"
        return "INTOSC2" if c & OSCCLKSRC2SEL else "EXT"

    def _src_hz(self, src: str) -> float:
        if src == "INTOSC1":
            return self.intosc1_hz
        if src == "INTOSC2":
            return self.intosc2_hz
        return self.ext_source()[1] or self.intosc1_hz       # nominal value only used for timing formulas

    def _src_running(self, src: str) -> bool:
        c = self._clkctl()
        halt = self.lpm == "HALT"
        if src == "INTOSC1":
            return self.intosc_ok[1] and not c & INTOSC1OFF and not (halt and not c & INTOSC1HALTI)
        if src == "INTOSC2":
            return self.intosc_ok[2] and not c & INTOSC2OFF and not (halt and not c & INTOSC2HALTI)
        if halt:
            return False
        name, hz = self.ext_source()
        return bool(hz) and self.ext_ok and name != "external"

    def oscclk_hz(self) -> float:
        return self._src_hz(self.source())

    def osc_fed(self) -> bool:
        return self._src_running(self.source()) and not self.get("PLLSTS") & OSCOFF

    def wd_source(self) -> str:
        if not self._clkctl() & WDCLKSRCSEL:
            return "INTOSC1"
        return "INTOSC2" if self._clkctl() & OSCCLKSRC2SEL else "EXT"

    def _wd_clock_on(self) -> bool:
        """The CPU watchdog's clock runs: its source oscillator runs (in HALT only with INTOSCnHALTI) and, in HALT,
        CLKCTL.WDHALTI = 1 (SPRUI09 Table 1-21, 1.3.3.2)."""
        if self.lpm == "HALT" and not self._clkctl() & WDHALTI:
            return False
        return self._src_running(self.wd_source())

    def sysclk_hz(self) -> float:
        """SPRUI09 Table 1-23 (DIVSEL 0/1 -> /4, 2 -> /2, 3 -> /1, with or without the PLL); limp mode: the PLL limp
        clock (`limp_hz`) through the same divider (SPRS523 8.13.1.4: 1-5 MHz with /2)."""
        n = _DIVSEL_DIV[self._divsel()]
        if self.limp:
            return self.limp_hz / n
        div = self._locked_div
        osc = self.oscclk_hz()
        return (osc if div == 0 else osc * div) / n

    core_hz = sysclk_hz

    def wd_period_ns(self) -> float:
        ps = self.get("WDCR") & 7
        presc = 1 if ps <= 1 else 1 << (ps - 1)
        return 512.0 * presc * 1e9 / self._src_hz(self.wd_source())

    def _wd_running(self) -> bool:
        return self.wd_enabled() and not self._wd_held

    # ------------------------------------------------------------------ lifecycle
    def attach(self, chip: Any) -> None:
        super().attach(chip)

    def reset(self) -> None:
        arch = getattr(self.chip, "arch", None) if self.chip is not None else None
        why = getattr(arch, "reset_requested", None)
        keep_clk = self._mclkres and "CLKCTL" in self.regs
        clkctl = self.get("CLKCTL") if keep_clk else None
        lockprd = self.get("PLLLOCKPRD") if "PLLLOCKPRD" in self.regs and why not in (None, "watchdog") else None
        super().reset()
        if clkctl is not None:
            self.set("CLKCTL", clkctl & ~WDCLKSRCSEL)
        if lockprd is not None:
            self.set("PLLLOCKPRD", lockprd)      # PLLLOCKPRD is reset by XRS only (SPRUI09 Table 1-25)
        self._osc_changed()

    # ------------------------------------------------------------------ test / board hooks
    def stop_oscillator(self) -> None:
        """The external clock (crystal or XCLKIN) fails."""
        if self.ext_ok:
            self.ext_ok = False
            self.emit("info", block=self.name, msg="external oscillator stopped")
            self._osc_changed()

    def start_oscillator(self) -> None:
        if not self.ext_ok:
            self.ext_ok = True
            self.emit("info", block=self.name, msg="external oscillator running")
            self._osc_changed()

    def fail_intosc(self, n: int, failed: bool = True) -> None:
        """INTOSCn fails (or recovers): test hook for missing-clock cases A/B."""
        self.intosc_ok[n] = not failed
        self._osc_changed()

    # ------------------------------------------------------------------ missing clock
    def _osc_changed(self) -> None:
        if self.chip is None:
            return
        self._wd_hold(not self._wd_clock_on())
        if self.lpm == "HALT":
            return
        sched = self.chip.sched
        fed = self.osc_fed()
        if fed:
            if self._mclk_ev is not None:
                sched.cancel(self._mclk_ev)
                self._mclk_ev = None
            if self._dead:
                self._dead = False
                self.emit("info", block=self.name, msg="OSCCLK back: CPU clock restarts")
                if self.lpm is None and self.chip.wake_gate == self.wake_gate:
                    self.chip.wake_gate = None
                self.chip.wake()
            return
        if self.get("PLLSTS") & MCLKOFF:
            if not self._dead and not self.limp:
                self._clock_dead("OSCCLK missing with PLLSTS.MCLKOFF = 1 (no limp-mode clock)")
            return
        if self.limp or self._mclk_ev is not None:
            return
        t = float(self.mclk_detect_ns) if self.mclk_detect_ns is not None else VCOCLK_COUNTER * 1e9 / self.limp_hz
        self._mclk_ev = sched.in_ns(t, self._mclk_detected)

    def _nmi(self):
        return find_model(self.chip, "ti_c2000.nmiwd")

    def _mclk_detected(self) -> None:
        self._mclk_ev = None
        if self.osc_fed() or self.limp:
            return
        self.clockfails += 1
        src = self.source()
        ctl = self._clkctl()
        self.set("PLLSTS", self.get("PLLSTS") | MCLKSTS)
        nmi = self._nmi()
        if nmi is not None:
            nmi.clock_fail()
        self._tz5(True)
        if not ctl & NMIRESETSEL:                       # cases A / C: MCLKRS at once
            self.emit("reset", block=self.name, reason="missing_clock",
                      msg=f"missing OSCCLK ({src}) detected: CLOCKFAIL and missing-clock reset (MCLKRS, "
                          "CLKCTL.NMIRESETSEL = 0), CPU in limp mode")
            self._mclkres = True
            self.chip.arch.reset_requested = "missing_clock"
            core = self.chip.core
            if getattr(core, "running", False):
                core.request_stop(from_mmio=True)
            return
        if src != "INTOSC1" and self._src_running("INTOSC1"):       # case D: switch to INTOSC1
            self.set("CLKCTL", ctl & ~(OSCCLKSRCSEL | WDCLKSRCSEL))
            self.set("PLLCR", 0)
            self._locked_div = 0
            if self._lock_ev is not None:
                self.chip.sched.cancel(self._lock_ev)
                self._lock_ev = None
            self.set("PLLSTS", (self.get("PLLSTS") | 1) & ~MCLKSTS)
            self.emit("warn", block=self.name, msg=f"missing OSCCLK ({src}): CLOCKFAIL, OSCCLK switched to INTOSC1, "
                      "PLLCR = 0 (NMIRESETSEL = 1)")
            self._apply_clock("clock fail: INTOSC1")
            self._wd_restart(self.wd_count())
            return
        self.limp = True                                            # case B: limp mode
        self.emit("warn", block=self.name, msg=f"missing OSCCLK ({src}): CLOCKFAIL, CPU in limp mode "
                  "(NMIRESETSEL = 1: NMI watchdog running)")
        self._apply_clock("limp mode")

    def nmi_reset(self) -> None:
        """NMIRS from the NMI watchdog: an internal missing-clock reset (MCLKRS)."""
        self.emit("reset", block=self.name, reason="nmi_watchdog", msg="NMI watchdog expired: NMIRS -> MCLKRS")
        self._mclkres = bool(self.get("PLLSTS") & MCLKSTS) or self.limp
        self.chip.arch.reset_requested = "nmi_watchdog"
        core = self.chip.core
        if getattr(core, "running", False):
            core.request_stop(from_mmio=True)

    def _tz5(self, on: bool) -> None:
        for b in self.chip.peripherals.values():
            f = getattr(b, "clockfail_changed", None)
            if f is not None:
                f(on)

    def clockfail(self) -> bool:
        nmi = self._nmi()
        return bool(nmi is not None and nmi.get("NMIFLG") & 2) or self.limp

    # ------------------------------------------------------------------ registers
    def _clkctl_write(self, blk, reg, old, new):
        new &= 0xFFFF
        ch = old ^ new
        self.set("CLKCTL", new)
        if ch & 0xF8 and new & 0xF8 and not self._tmr2_warned:
            self._tmr2_warned = True
            self.emit("warn", block=self.name, msg="CLKCTL.TMR2CLKSRCSEL/TMR2CLKPRESCALE: CPU timer 2 is modelled "
                      "on SYSCLKOUT only")
        if ch & OSCCLKSRCSEL:                         # source switch: PLLCR forced to 0 (SPRUI09 Table 1-21)
            if self._lock_ev is not None:
                self.chip.sched.cancel(self._lock_ev)
                self._lock_ev = None
            self.set("PLLCR", 0)
            self._locked_div = 0
            self.set("PLLSTS", self.get("PLLSTS") | 1)
        cnt = self.wd_count()
        if ch & (OSCCLKSRCSEL | OSCCLKSRC2SEL):
            self.emit("info", block=self.name, msg=f"OSCCLK source {self.source()} "
                      f"({fmt_hz(self.oscclk_hz())}{'' if self.osc_fed() else ', not running'})")
        if ch:
            self._apply_clock("CLKCTL")
            self._wd_restart(cnt)
            self._osc_changed()
        return new

    def _xclk_write(self, blk, reg, old, new):
        self.set("XCLK", new & 0x43)
        if (old ^ new) & XCLKINSEL:
            self._osc_changed()
        return new & 0x43

    def _pllcr_write(self, blk, reg, old, new):
        """PLLCR change procedure (SPRUI09 Figure 1-23): DIVSEL must be 0/1 and MCLKSTS 0 before the write; the CPU runs
        in bypass (OSCCLK / DIVSEL) for PLLLOCKPRD OSCCLK cycles, then PLLLOCKS = 1 and the new SYSCLKOUT applies."""
        div = new & self.div_mask
        if div > self.max_div:
            self.emit("warn", block=self.name, msg=f"PLLCR.DIV={div} is reserved (max {self.max_div})")
        if self._divsel() not in (0, 1) and div != old & self.div_mask:
            self.emit("warn", block=self.name, msg="PLLCR written while PLLSTS.DIVSEL != 0 (SPRUI09 Figure 1-23: set "
                      "DIVSEL to 0 before writing PLLCR)")
        if self.get("PLLSTS") & (1 << 2) and div:
            self.emit("warn", block=self.name, msg="PLLCR written non-zero with PLLSTS.PLLOFF = 1 (SPRUI09: do not)")
        if div == (old & self.div_mask) and self._lock_ev is None:
            return div
        if self.limp or not self.osc_fed():
            self.emit("warn", block=self.name, msg="PLLCR written in limp mode / without OSCCLK (SPRUI09 1.3.2.6: the "
                      "clocks stop)")
            if not self.osc_fed():
                self.set("PLLCR", div)
                self._clock_dead("PLLCR written without OSCCLK")
                return div
        self._locked_div = 0
        self.set("PLLSTS", self.get("PLLSTS") & ~1)
        self._apply_clock("PLL relock (bypass)")
        if self._lock_ev is not None:
            self.chip.sched.cancel(self._lock_ev)
        prd = self.get("PLLLOCKPRD") if "PLLLOCKPRD" in self.regs else 65535
        t = float(self.pll_lock_ns) if self.pll_lock_ns is not None else prd * 1e9 / max(self.oscclk_hz(), 1.0)
        self._lock_ev = self.chip.sched.in_ns(t, self._pll_locked, div)
        return div


@register("ti_c2000.nmiwd")
class NmiWatchdog(C2000Block):
    """NMI configuration / flags and the NMI watchdog (0x7060).  See the module docstring."""
    eallow_regs = ("NMICFG", "NMIFLG", "NMIFLGCLR", "NMIFLGFRC", "NMIWDPRD")
    cycle_anchors = ("_c0",)

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        ov = {"NMICFG": {"on_write": self._cfg_write},
              "NMIFLG": {"ro_mask": 0xFFFF},
              "NMIFLGCLR": {"on_write": self._clr_write, "on_read": lambda b, r: 0, "on_peek": lambda b, r: 0},
              "NMIFLGFRC": {"on_write": self._frc_write, "on_read": lambda b, r: 0, "on_peek": lambda b, r: 0},
              "NMIWDCNT": {"on_read": self._cnt_read, "on_peek": self._cnt_read, "ro_mask": 0xFFFF},
              "NMIWDPRD": {"on_write": self._prd_write}}
        regs = build_regs((name, "NMIINT"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, regs, params=params, log=log)
        self._ev = None
        self._c0: int | None = None          # cycle at which the counter was 0 (None: not counting)
        self._cnt0 = 0
        self.nmis = 0

    def reset(self) -> None:
        arch = getattr(self.chip, "arch", None) if self.chip is not None else None
        why = getattr(arch, "reset_requested", None)
        flg = self.get("NMIFLG") if why not in (None, "watchdog") else 0   # NMIFLG is reset by XRS only
        if self._ev is not None and self.chip is not None:
            self.chip.sched.cancel(self._ev)
        self._ev = None
        self._c0 = None
        self._cnt0 = 0
        super().reset()
        if flg:
            self.set("NMIFLG", flg)
            self._update()

    # ------------------------------------------------------------------ counter
    def _counting(self) -> bool:
        return bool(self.get("NMIFLG") & self.get("NMICFG") & 2)

    def count(self) -> int:
        if self._c0 is None or self.chip is None:
            return self._cnt0
        return (self._cnt0 + self.chip.now_cycles() - self._c0) & 0xFFFF

    def _update(self) -> None:
        """Start/stop the counter after a flag or configuration change and schedule the NMIRS."""
        if self.chip is None:
            return
        if self._ev is not None:
            self.chip.sched.cancel(self._ev)
            self._ev = None
        if not self._counting():
            self._c0, self._cnt0 = None, 0       # "the counter resets to zero and remains at zero"
            return
        now = self.chip.now_cycles()
        if self._c0 is None:
            self._c0, self._cnt0 = now, 0
        cnt = self.count()
        prd = self.get("NMIWDPRD")
        dist = (prd - cnt) & 0xFFFF or 0x10000
        self._ev = self.chip.sched.at_cycles(now + dist, self._expire)

    def _expire(self) -> None:
        self._ev = None
        self._c0, self._cnt0 = self.chip.now_cycles(), 0
        sc = find_model(self.chip, "ti_c2000.sysctrl")
        if sc is not None and hasattr(sc, "nmi_reset"):
            sc.nmi_reset()

    def clock_fail(self) -> None:
        """CLOCKFAIL from the missing-clock logic."""
        self.set("NMIFLG", self.get("NMIFLG") | 2)
        self._raise()

    def _raise(self) -> None:
        flg = self.get("NMIFLG")
        if flg & 2 and self.get("NMICFG") & 2 and not flg & 1:
            self.set("NMIFLG", flg | 1)
            self.nmis += 1
            self.pulse_irq("NMI")
        self._update()

    def _cfg_write(self, blk, reg, old, new):
        v = old | (new & 2)                          # CLOCKFAIL enable: writes of 0 are ignored
        self.set("NMICFG", v)
        if v != old:
            self._raise()
        return v

    def _clr_write(self, blk, reg, old, new):
        flg = self.get("NMIFLG") & ~(new & 3)
        self.set("NMIFLG", flg)
        if new & 1 and flg & 2 and self.get("NMICFG") & 2:
            self._raise()                            # CLOCKFAIL still pending: another NMI
        self._update()
        return 0

    def _frc_write(self, blk, reg, old, new):
        if new & 2:
            self.clock_fail()
        return 0

    def _cnt_read(self, blk, reg):
        return self.count()

    def _prd_write(self, blk, reg, old, new):
        new &= 0xFFFF
        self.set("NMIWDPRD", new)
        if self._counting() and new == self.count():
            self._expire()                           # "writing a PERIOD equal to the counter forces an NMIRS"
            return new
        self._update()
        return new
