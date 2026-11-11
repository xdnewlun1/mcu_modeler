"""P89CV51 system control: PCON (87H), AUXR (8EH), CKCON (8FH) — power-saving modes, clock mode, expanded RAM.

* PCON (Table 4 p.11; sec. 6.12 pp.56-57): IDL = 1 enters Idle after the writing instruction (`core.enter_sleep`
  -> StopReason.WFI, stop_info "idle"): the CPU stops, clocks and peripherals run (the PCA stops when CMOD.CIDL = 1);
  any enabled interrupt (flag & IE bit & EA) ends it and the "start of interrupt clears IDL".  PD = 1 (wins over
  IDL) enters Power-down: the oscillator stops, so every timer/UART/PCA/SPI/watchdog freezes (chipem/periph/
  lowpower.Freezer: timers, PCA, watchdog get `clock_stop(True, "pd")`; the pending events of the UART and SPI are
  held and resumed with their remaining time, and they ignore input while `lp_frozen`); only an enabled LEVEL-sensitive external interrupt (INTx low with
  ITx = 0, EXx = 1, EA = 1) or a reset ends it (`chip.wake_gate`).  Sec. 6.12.2: "Holding an external interrupt
  pin LOW restarts the oscillator, the signal must hold LOW at least 1024 clock cycles before bringing back HIGH to
  complete the exit": the wake-up completes `params.pd_wake_clocks` (default 1024) oscillator periods after the
  level interrupt became pending; released earlier, the device stays in Power-down (one `warn`).  POF (bit 4) is set by a power-on reset only and kept by other resets; SMOD1
  doubles the UART mode 1/2/3 baud rate, SMOD0 makes SCON.7 the framing-error flag (read by mcs51.uart).
* AUXR (Table 5/6 pp.13-14): EXTRAM = 1 sends every MOVX to the external bus (`core.extram`); with EXTRAM = 0 MOVX
  below 0300H uses the 768-byte on-chip XRAM.  AO = 1 stops ALE except during MOVX/MOVC (the ALE pin is not
  toggled by this model either way: recorded only).
* CKCON (Table 50-52 p.58): X2 = 1 (or the FX2 flash bit, mcs51.flash) puts the CPU in 6-clock mode
  (`core.set_clocks_per_mc(6)`); then each peripheral counts every 6 oscillator periods unless its own CKCON bit
  (SPIX2, WDX2, PCAX2, SIX2, T2X2, T1X2, T0X2) is 1 (12 periods).  In 12-clock mode everything uses 12.  A change
  re-bases the time-derived counters of every model with `clock_rebase()` / `clock_resume()`.
"""
from __future__ import annotations

from typing import Any

from ..registry import register
from ._util import SfrBlock, find_model

IDL, PD, GF0, GF1, POF, SMOD0, SMOD1 = 1, 2, 4, 8, 0x10, 0x40, 0x80
EXTRAM, AO = 2, 1
CKCON_BITS = {"X2": 0, "T0X2": 1, "T1X2": 2, "T2X2": 3, "SIX2": 4, "PCAX2": 5, "WDX2": 6, "SPIX2": 7}


@register("mcs51.sysctrl")
class SysCtrl(SfrBlock):
    model = "mcs51.sysctrl"
    table = ("SYSCTRL",)

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        ov = {"PCON": {"on_write": self._pcon_w, "ro_mask": 0x20},
              "AUXR": {"on_write": self._auxr_w, "ro_mask": 0xFC},
              "CKCON": {"on_write": self._ckcon_w}}
        super().__init__(name, base, size, regs, params=params, log=log, overrides=ov)
        self.mode: str | None = None         # "idle" | "pd" while sleeping
        self.fx2 = False                     # FX2 non-volatile 6-clock bit (mcs51.flash sets it)
        self._pof = True
        self.pd_wake_clocks = int(self.params.get("pd_wake_clocks", 1024))
        self._osc_start: int | None = None   # clock cycle the oscillator restarted (power-down wake request)
        self._osc_ev = None

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        chip.power_hooks.append(self)

    def reset(self) -> None:
        fz = getattr(self, "_freezer", None)
        if fz is not None:
            fz.drop()
            self._freezer = None
        self.cancel(getattr(self, "_osc_ev", None))
        self._osc_ev = None
        self._osc_start = None
        pof = self._pof
        cause = getattr(getattr(self.chip, "arch", None), "reset_cause", "por") if self.chip else "por"
        super().reset()
        if cause == "por":
            pof = True
        if not self.params.get("pof", True):            # PCON.POF is an NXP addition (not on the Intel 80C52)
            pof = False
        self.set("PCON", POF if pof else 0)
        self._pof = pof
        self.mode = None
        if self.chip is not None:
            core = self.chip.core
            if hasattr(core, "extram"):
                core.extram = False
            fl = find_model(self.chip, "mcs51.flash")
            self.fx2 = bool(getattr(fl, "fx2", False))
            self._apply_cpu_clock()

    # ------------------------------------------------------------------ clocks
    def _opt(self, name: str) -> int:
        """Value of an SFR this part may not have (AUXR, CKCON: absent on a plain 80C52) - 0 when absent."""
        return self.get(name) if name in self.regs else 0

    def cpu_x2(self) -> bool:
        return self.fx2 or bool(self._opt("CKCON") & 1)

    def periph_div(self, bit: str | None = None) -> int:
        """Oscillator periods per peripheral clock cycle: 12, or 6 in 6-clock mode unless the CKCON bit is set."""
        if not self.cpu_x2():
            return 12
        if bit is None or bit == "X2":
            return 6
        return 12 if self._opt("CKCON") >> CKCON_BITS[bit] & 1 else 6

    def _apply_cpu_clock(self) -> None:
        f = getattr(self.chip.core, "set_clocks_per_mc", None)
        if f is not None:
            f(6 if self.cpu_x2() else 12)

    def _blocks(self):
        return [b for b in self.chip.peripherals.values() if b is not self and str(getattr(b, "model", "")).startswith("mcs51.")]

    def _ckcon_w(self, blk, reg, old, new):
        if old == new:
            return None
        self.set("CKCON", old)
        for b in self._blocks():
            f = getattr(b, "clock_rebase", None)
            if f is not None:
                f()
        self.set("CKCON", new)
        self._apply_cpu_clock()
        for b in self._blocks():
            f = getattr(b, "clock_resume", None)
            if f is not None:
                f()
        if (old ^ new) & 1:
            self.emit("info", block=self.name, msg=f"CPU clock mode: {6 if self.cpu_x2() else 12} clocks per "
                      "machine cycle")
        return None

    def set_fx2(self, on: bool) -> None:
        if bool(on) == self.fx2:
            return
        for b in self._blocks():
            f = getattr(b, "clock_rebase", None)
            if f is not None:
                f()
        self.fx2 = bool(on)
        self._apply_cpu_clock()
        for b in self._blocks():
            f = getattr(b, "clock_resume", None)
            if f is not None:
                f()

    # ------------------------------------------------------------------ AUXR
    def _auxr_w(self, blk, reg, old, new):
        core = self.chip.core
        if hasattr(core, "extram"):
            core.extram = bool(new & EXTRAM)
        return None

    # ------------------------------------------------------------------ PCON / power modes
    def _pcon_w(self, blk, reg, old, new):
        self._pof = bool(new & POF)
        if new & (IDL | PD):
            mode = "pd" if new & PD else "idle"
            f = getattr(self.chip.core, "enter_sleep", None)
            if f is not None:
                f(mode)
        if (old ^ new) & SMOD1:
            u = find_model(self.chip, "mcs51.uart")
            if u is not None:
                u.config_changed()
        return None

    def smod1(self) -> bool:
        return bool(self.get("PCON") & SMOD1)

    def smod0(self) -> bool:
        return bool(self.get("PCON") & SMOD0)

    def on_sleep(self, chip: Any) -> None:
        mode = getattr(chip.core, "stop_info", None)
        if mode not in ("idle", "pd"):
            mode = "pd" if self.get("PCON") & PD else "idle"
        self.mode = mode
        self.emit("info", block=self.name, msg=f"{'Power-down' if mode == 'pd' else 'Idle'} mode entered",
                  mode=mode)
        if mode == "pd":
            # oscillator stopped: models with clock_stop freeze their counters, the pending events of the others
            # (UART frames, SPI edges) are held and resumed with their remaining time (chipem/periph/lowpower.py)
            from ..lowpower import Freezer
            self._freezer = Freezer(chip)
            self._freezer.freeze(self._blocks(), "pd")
        else:
            for b in self._blocks():
                f = getattr(b, "clock_stop", None)
                if f is not None:
                    f(True, mode)
        chip.wake_gate = self._pd_gate if mode == "pd" else None

    def _pd_gate(self) -> bool:
        t = find_model(self.chip, "mcs51.timer01")
        req = bool(t is not None and t.pd_wakeup())
        now = self.chip.clock.cycles
        if not req:
            if self._osc_start is not None:
                self.emit("warn", block=self.name, msg=f"external interrupt released {now - self._osc_start} clocks "
                          f"after it restarted the oscillator (< {self.pd_wake_clocks}): Power-down not left")
                self._osc_start = None
                self.cancel(self._osc_ev)
                self._osc_ev = None
            return False
        if self.pd_wake_clocks <= 0:
            return True
        if self._osc_start is None:
            self._osc_start = now
            self._osc_ev = self.chip.sched.at_cycles(now + self.pd_wake_clocks, self._osc_ready)
        return now >= self._osc_start + self.pd_wake_clocks

    def _osc_ready(self) -> None:
        """Oscillator restart time over: an event at that cycle makes the sleeping Chip loop re-check the gate; if
        the interrupt pin was released meanwhile the exit did not complete (the gate is only consulted while a wake
        request is pending, so the release is noticed here)."""
        self._osc_ev = None
        t = find_model(self.chip, "mcs51.timer01")
        if self.mode == "pd" and self._osc_start is not None and not (t is not None and t.pd_wakeup()):
            self.emit("warn", block=self.name, msg=f"external interrupt released less than {self.pd_wake_clocks} "
                      "clocks after it restarted the oscillator: Power-down not left")
            self._osc_start = None

    def on_wake(self, chip: Any) -> None:
        mode = self.mode
        self.mode = None
        chip.wake_gate = None
        self._osc_start = None
        self.cancel(self._osc_ev)
        self._osc_ev = None
        self.set("PCON", self.get("PCON") & ~(IDL | PD))
        fz = getattr(self, "_freezer", None)
        if fz is not None:
            fz.thaw()
            self._freezer = None
        else:
            for b in self._blocks():
                f = getattr(b, "clock_stop", None)
                if f is not None:
                    f(False, mode or "idle")
        self.emit("info", block=self.name, msg=f"{'Power-down' if mode == 'pd' else 'Idle'} mode left", mode=mode)
