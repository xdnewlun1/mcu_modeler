"""C2833x/C2823x system control (0x7010): PLL and clock tree, peripheral clock gates, watchdog.

Clock tree (SPRS439 8.6, SPRUFB0 ch. 1):
  OSCCLK = params.ext_hz (crystal / XCLKIN, default 30 MHz)
  SYSCLKOUT = OSCCLK × PLLCR.DIV / {4 (DIVSEL 0/1), 2 (DIVSEL 2)}; with DIV = 0 (bypass) OSCCLK / {4, 2, 1 (DIVSEL 3)}
  HSPCLK = SYSCLKOUT / (2 × HISPCP) (HISPCP = 0 → /1);  LSPCLK = SYSCLKOUT / (2 × LOSPCP) (reset LOSPCP = 2 → /4)
After a PLLCR write the PLL relocks for 131072 OSCCLK cycles (PLLSTS.PLLLOCKS = 0, CPU in bypass); the new
SYSCLKOUT applies when PLLLOCKS returns to 1.  SYSCLKOUT drives `chip.clock` (the core clock) unless `frozen`
(the --clock override).  The boot ROM leaves DIVSEL = 2 (/2); we skip the boot ROM, so `params.boot_divsel`
(default 2) is applied after every reset.

Watchdog (SPRUI07 1.3.4): WDCLK = OSCCLK / 512 / prescale (WDCR.WDPS 0,1 → /1, n → /2^(n-1)); 8-bit WDCNTR counts
up and at 0xFF→0 resets the chip (`reset` event, WDFLAG set afterwards, cleared only by writing 1 or by XRS) or,
with SCSR.WDENINT, drives WDINT low for 512 OSCCLK: WAKEINT (PIE 1.8) is pulsed on the falling edge and SCSR.WDINTS
reads 0 while WDINT is low; clearing WDENINT then resets the chip at once.  Writing 0x55 then 0xAA to WDKEY clears
the counter (any other value disarms the sequence); WDKEY reads return WDCR.  WDCR writes must carry WDCHK = 101,
otherwise the chip resets (or WDINT fires with WDENINT = 1) at once, even with the watchdog disabled.  WDDIS can only
change while SCSR.WDOVERRIDE = 1 (write 1 to clear it, locking WDDIS).  The watchdog needs OSCCLK: it stops while
the oscillator is missing (limp mode) and in HALT.  EALLOW protects every register here.

Low-power modes (SPRUI07 1.3.3, LPMCR0 + GPIOLPMSEL; `chip.power_hooks`): the IDLE instruction with LPM = 00 is
plain IDLE (any enabled interrupt wakes the core).  LPM = 01 STANDBY: SYSCLKOUT stops for every peripheral
(`C2000Block.clock_stop`: events parked, counters frozen, pin inputs ignored, SCI bytes dropped), the oscillator, PLL
and watchdog keep running; wake-up by a GPIOLPMSEL-selected port A pin held low for QUALSTDBY + 2 OSCCLK cycles, or by
the watchdog interrupt when LPMCR0.WDINTE = 1 (and SCSR.WDENINT = 1), or XRS.  LPM = 1x HALT: oscillator, PLL and
watchdog stop too; a selected pin going low restarts the oscillator (`params.osc_startup_ns`) and relocks the PLL
(131072 OSCCLK unless PLLOFF = 1); the pin must still be low when the oscillator is up.  On exit the clocks restart
and WAKEINT is latched in the PIE; the CPU leaves IDLE only through an enabled interrupt (PIEIER1.8 + IER.INT1),
otherwise it stays in IDLE (HALT: one `warn`).  Interrupts other than WAKEINT do not end STANDBY/HALT.

Missing-clock detection (SPRUI07 1.3.2.5): `stop_oscillator()` / `start_oscillator()` (tests, board scripts) or
PLLSTS.OSCOFF = 1 remove OSCCLK from the PLL.  With MCLKOFF = 0 the detector fires after `params.mclk_detect_ns`:
PLLSTS.MCLKSTS = 1 and a missing-clock reset (MCLKRES: internal, PLLCR/PLLSTS and WDFLAG kept), after which the CPU
runs at the PLL limp frequency (`params.limp_hz`): limp/2 with the PLL enabled, limp / DIVSEL divider in bypass.
MCLKCLR clears MCLKSTS and re-arms the detector (another MCLKRES if OSCCLK is still missing); XRS clears it too.
With MCLKOFF = 1 there is no limp clock and the CPU stops until OSCCLK returns.
"""
from __future__ import annotations

from typing import Any

from ..registry import register
from ._util import C2000Block, build_regs, fmt_hz

GATES = {
    "PCLKCR0": {"TBCLKSYNC": 2, "ADCENCLK": 3, "I2CAENCLK": 4, "SCICENCLK": 5, "SPIAENCLK": 8, "SCIAENCLK": 10,
                "SCIBENCLK": 11, "MCBSPAENCLK": 12, "MCBSPBENCLK": 13, "ECANAENCLK": 14, "ECANBENCLK": 15},
    "PCLKCR1": {**{f"EPWM{i}ENCLK": i - 1 for i in range(1, 7)}, **{f"ECAP{i}ENCLK": 7 + i for i in range(1, 7)},
                "EQEP1ENCLK": 14, "EQEP2ENCLK": 15},
    "PCLKCR3": {"CPUTIMER0ENCLK": 8, "CPUTIMER1ENCLK": 9, "CPUTIMER2ENCLK": 10, "DMAENCLK": 11, "XINTFENCLK": 12,
                "GPIOINENCLK": 13},
}
_GATE_OF = {g: (reg, bit) for reg, d in GATES.items() for g, bit in d.items()}
PLL_LOCK_OSC_CYCLES = 131072
OSCOFF, MCLKOFF, MCLKSTS, MCLKCLR, PLLOFF = 1 << 5, 1 << 6, 1 << 3, 1 << 4, 1 << 2
WDINT_OSC_CYCLES = 512


@register("ti_c2000.sysctrl")
class SysCtrl(C2000Block):
    eallow_regs = "*"

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        ov = {
            "PLLSTS": {"on_write": self._pllsts_write},
            "PLLCR": {"on_write": self._pllcr_write},
            "HISPCP": {"on_write": self._presc_write},
            "LOSPCP": {"on_write": self._presc_write},
            "SCSR": {"on_write": self._scsr_write},
            "WDCNTR": {"on_read": self._wdcntr_read},
            "WDKEY": {"on_write": self._wdkey_write, "on_read": self._wdkey_read},
            "WDCR": {"on_write": self._wdcr_write},
            "PCLKCR0": {"on_write": self._pclk_write},
            "PCLKCR1": {"on_write": self._pclk_write},
            "PCLKCR3": {"on_write": self._pclk_write},
        }
        regs = build_regs((name, "SYSCTRL"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, regs, params=params, log=log)
        self.ext_hz = float(params.get("ext_hz", 30_000_000))
        self.boot_divsel = int(params.get("boot_divsel", 2))
        self.pll_lock_ns = params.get("pll_lock_ns")
        self.frozen = False                 # set by the session for --clock
        self.pclk_listeners: list = []      # cb() after a PCLKCRx write (ePWM: TBCLKSYNC / EPWMxENCLK)
        self._lock_ev = None
        self._wd_ev = None
        self._wd_base_t = 0.0               # wall time at which WDCNTR was _wd_base_cnt
        self._wd_base_cnt = 0
        self._wd_key55 = False
        self._wd_reset_flag = False         # survives the watchdog reset → WDFLAG
        self._locked_div = 0                # PLLCR.DIV currently in effect (bypass while relocking)
        # watchdog interrupt line (WDINT low for 512 OSCCLK) and hold (no OSCCLK)
        self._wdint_ev = None
        self._wd_held = False
        # oscillator / missing-clock detection
        self.limp_hz = float(params.get("limp_hz", 4_000_000))
        self.mclk_detect_ns = params.get("mclk_detect_ns")
        self.osc_startup_ns = float(params.get("osc_startup_ns", 0.0))
        self.osc_ok = True                  # external clock source present (stop_oscillator / start_oscillator)
        self.limp = False                   # CPU clocked by the PLL limp frequency (PLLSTS.MCLKSTS)
        self._mclk_ev = None
        self._mclkres = False               # the pending reset is a missing-clock reset (PLLCR/PLLSTS kept)
        self._dead = False                  # no clock at all (MCLKOFF = 1 with OSCCLK missing): CPU stopped
        # low-power modes
        self.lpm: str | None = None         # "STANDBY" / "HALT" while in that mode
        self._lpm_pins: list = []
        self._lpm_ev = None                 # qualification (STANDBY) or oscillator start / PLL lock (HALT)
        self._lpm_pin = None
        self.lpm_entries = {"IDLE": 0, "STANDBY": 0, "HALT": 0}

    def _pclk_write(self, blk, reg, old, new):
        if old != new:
            self._store(reg, new)
            for cb in list(self.pclk_listeners):
                cb()
        return None

    # ------------------------------------------------------------------ clocks
    def osc_hz(self) -> float:
        return self.ext_hz

    def _divsel(self) -> int:
        return (self.get("PLLSTS") >> 7) & 3

    def sysclk_hz(self) -> float:
        div = self._locked_div
        ds = self._divsel()
        osc = self.ext_hz
        if self.limp:
            if (self.get("PLLCR") & 0xF) != 0:
                return self.limp_hz / 2
            return self.limp_hz / (4 if ds < 2 else (2 if ds == 2 else 1))
        if div == 0:
            return osc / (4 if ds < 2 else (2 if ds == 2 else 1))
        return osc * div / (4 if ds < 2 else 2)

    core_hz = sysclk_hz

    def hspclk_hz(self) -> float:
        p = self.get("HISPCP") & 7
        return self._sysclk_now() / (2 * p if p else 1)

    def lspclk_hz(self) -> float:
        p = self.get("LOSPCP") & 7
        return self._sysclk_now() / (2 * p if p else 1)

    def _sysclk_now(self) -> float:
        return float(self.chip.clock.hz) if self.chip is not None else self.sysclk_hz()

    def clock_enabled(self, gate: str) -> bool:
        g = _GATE_OF.get(gate)
        if g is None:
            return True
        reg, bit = g
        return bool(self.get(reg) >> bit & 1)

    def _apply_clock(self, why: str = "") -> None:
        if self.chip is None:
            return
        hz = self.sysclk_hz()
        if self.frozen or getattr(self.chip, "clock_override", None):
            return
        if self.chip.clock.hz != hz:
            self.chip.clock.set_hz(hz)
            self.emit("info", block=self.name, msg=f"SYSCLKOUT {fmt_hz(hz)}{' (' + why + ')' if why else ''}")

    # ------------------------------------------------------------------ lifecycle
    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._sysctrl = self
        hooks = getattr(chip, "power_hooks", None)
        if hooks is not None and self not in hooks:
            hooks.append(self)

    def reset(self) -> None:
        chip = self.chip
        mclkres = self._mclkres
        keep = (self.get("PLLCR"), self.get("PLLSTS"), self._locked_div, self.get("WDCR") & 0x80) if mclkres else None
        if chip is not None:
            for ev in (self._lock_ev, self._wd_ev, self._wdint_ev, self._mclk_ev, self._lpm_ev):
                if ev is not None:
                    chip.sched.cancel(ev)
        self._lock_ev = self._wd_ev = self._wdint_ev = self._mclk_ev = self._lpm_ev = None
        self._lpm_leave_state()
        super().reset()
        self._mclkres = False
        self._wd_held = False
        if keep is not None:                       # MCLKRES: PLLCR / PLLSTS are not reset (XRS / watchdog only)
            self.set("PLLCR", keep[0])
            self.set("PLLSTS", (keep[1] | MCLKSTS) & ~MCLKCLR)
            self._locked_div = keep[2]
            self.set("WDCR", self.get("WDCR") | keep[3])
            self.limp = True
        else:
            self.limp = False
            self._dead = False
            self._locked_div = 0
            st = self.get("PLLSTS") & ~(3 << 7)
            self.set("PLLSTS", st | ((self.boot_divsel & 3) << 7))
        self._wd_key55 = False
        if self._wd_reset_flag:
            self.set("WDCR", self.get("WDCR") | 0x80)
            self._wd_reset_flag = False
        if chip is not None and getattr(chip, "wake_gate", None) == self.wake_gate:
            chip.wake_gate = None
        self._apply_clock("missing-clock reset (limp mode)" if mclkres else "reset")
        self._wd_restart(0)
        self._osc_changed()

    # ------------------------------------------------------------------ oscillator / missing-clock detection
    def stop_oscillator(self) -> None:
        """The external clock source (X1/X2 crystal or XCLKIN) fails (test / board stimulus)."""
        if self.osc_ok:
            self.osc_ok = False
            self.emit("info", block=self.name, msg="external oscillator stopped")
            self._osc_changed()

    def start_oscillator(self) -> None:
        if not self.osc_ok:
            self.osc_ok = True
            self.emit("info", block=self.name, msg="external oscillator running")
            self._osc_changed()

    def osc_fed(self) -> bool:
        """OSCCLK reaches the PLL / watchdog (source present, PLLSTS.OSCOFF = 0, not in HALT)."""
        return self.osc_ok and not self.get("PLLSTS") & OSCOFF and self.lpm != "HALT"

    def _osc_changed(self) -> None:
        if self.chip is None:
            return
        fed = self.osc_fed()
        self._wd_hold(not fed)
        sched = self.chip.sched
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
        if self.lpm == "HALT":
            return                                  # oscillator off on purpose
        if self.get("PLLSTS") & MCLKOFF:
            if not self._dead and not self.limp:
                self._clock_dead("OSCCLK missing with PLLSTS.MCLKOFF = 1 (no limp-mode clock)")
            return
        if self.limp or self._mclk_ev is not None:
            return
        t = float(self.mclk_detect_ns) if self.mclk_detect_ns is not None else 128 * 1e9 / self.limp_hz
        self._mclk_ev = sched.in_ns(t, self._mclk_detected)

    def _mclk_detected(self) -> None:
        self._mclk_ev = None
        if self.osc_fed() or self.limp:
            return
        self.set("PLLSTS", self.get("PLLSTS") | MCLKSTS)
        self.emit("reset", block=self.name, reason="missing_clock",
                  msg="missing OSCCLK detected: missing-clock reset (MCLKRES), CPU in limp mode")
        self._mclkres = True
        self.chip.arch.reset_requested = "missing_clock"
        core = self.chip.core
        if getattr(core, "running", False):
            core.request_stop(from_mmio=True)

    def _clock_dead(self, why: str) -> None:
        """No clock reaches the CPU: it stops (time still advances) until OSCCLK returns."""
        self._dead = True
        self.chip.wake_gate = self.wake_gate
        self.emit("warn", block=self.name, msg=f"CPU clock stopped: {why}")
        self.chip.sched.in_cycles(0, self._enter_dead)

    def _enter_dead(self) -> None:
        if self._dead and self.chip is not None:
            self.chip.sleeping = True               # held like a sleep with no wake-up source (wake_gate)

    # ------------------------------------------------------------------ low-power modes (chip.power_hooks)
    def wake_gate(self) -> bool:
        return self.lpm is None and not self._dead

    def on_sleep(self, chip: Any) -> None:
        if getattr(chip.core, "stop_info", None) != "idle":
            return
        lpm = self.get("LPMCR0") & 3
        if lpm == 0:
            self.lpm_entries["IDLE"] += 1
            return
        mode = "STANDBY" if lpm == 1 else "HALT"
        if mode == "HALT" and self.limp:
            self.emit("warn", block=self.name, msg="HALT requested in limp mode (PLLSTS.MCLKSTS = 1; SPRUI07: "
                      "unpredictable): entering STANDBY instead")
            mode = "STANDBY"
        if self.get("PLLSTS") & OSCOFF:
            self.emit("warn", block=self.name, msg=f"{mode} entered with PLLSTS.OSCOFF = 1 (SPRUI07: unpredictable)")
        self.lpm_entries[mode] += 1
        self.lpm = mode
        chip.wake_gate = self.wake_gate
        sel = self._lpmsel()
        self.emit("info", block=self.name, msg=f"{mode} entered (GPIOLPMSEL 0x{sel:08x}"
                  + (f", watchdog wake-up {'on' if self._wd_wakes() else 'off'}" if mode == "STANDBY" else "") + ")")
        for blk in self._clocked_blocks():
            blk.clock_stop(True, mode)
        if mode == "HALT":
            self._osc_changed()                     # watchdog held, no missing-clock detection
        self._watch_lpm_pins(sel)
        for p in self._lpm_pins:
            if p.level() == "L":
                self._lpm_pin_changed(p)
                break

    def on_wake(self, chip: Any) -> None:
        if self.lpm is not None:                    # woken by something else (debugger-like chip.wake()): resume
            self._lpm_exit(None, wakeint=False)

    def _clocked_blocks(self) -> list:
        chip = self.chip
        arch_blocks = getattr(chip, "arch_blocks", ())
        return [b for b in chip.peripherals.values()
                if b is not self and isinstance(b, C2000Block) and b not in arch_blocks]

    def _gpio(self):
        from ._util import find_model
        return find_model(self.chip, "ti_c2000.gpio", "GPIO")

    def _lpmsel(self) -> int:
        g = self._gpio()
        return g.get("GPIOLPMSEL") if g is not None and "GPIOLPMSEL" in g.regs else 0

    def _wd_wakes(self) -> bool:
        return bool(self.get("LPMCR0") & 0x8000) and bool(self.get("SCSR") & 2)

    def _watch_lpm_pins(self, sel: int) -> None:
        board = getattr(self.chip, "pins", None)
        if board is None:
            return
        for i in range(32):
            if sel >> i & 1:
                p = board.gpio("A", i)
                if p is not None:
                    self._lpm_pins.append(p)
                    board.input_listeners[p.id].append(self._lpm_pin_changed)

    def _lpm_leave_state(self) -> None:
        board = getattr(self.chip, "pins", None) if self.chip is not None else None
        if board is not None:
            for p in self._lpm_pins:
                lst = board.input_listeners.get(p.id)
                if lst is not None and self._lpm_pin_changed in lst:
                    lst.remove(self._lpm_pin_changed)
        self._lpm_pins = []
        self._lpm_pin = None
        if self.chip is not None and getattr(self.chip, "wake_gate", None) == self.wake_gate and not self._dead:
            self.chip.wake_gate = None
        self.lpm = None

    def _lpm_pin_changed(self, pin: Any) -> None:
        if self.lpm is None:
            return
        sched = self.chip.sched
        low = pin.level() == "L"
        if self.lpm == "STANDBY":
            if low and self._lpm_ev is None:
                q = (self.get("LPMCR0") >> 2) & 0x3F
                self._lpm_pin = pin
                self._lpm_ev = sched.in_ns((q + 2) * 1e9 / self.ext_hz, self._lpm_qualified, pin)
            elif not low and pin is self._lpm_pin and self._lpm_ev is not None:
                sched.cancel(self._lpm_ev)          # sampled high during the qualification: restart
                self._lpm_ev = None
                self._lpm_pin = None
                for p in self._lpm_pins:
                    if p.level() == "L":
                        self._lpm_pin_changed(p)
                        break
        elif low and self._lpm_ev is None:          # HALT: asynchronous, starts the oscillator
            self._lpm_pin = pin
            self._lpm_ev = sched.in_ns(self.osc_startup_ns, self._halt_osc_up, pin)

    def _lpm_qualified(self, pin: Any) -> None:
        self._lpm_ev = None
        if pin.level() == "L":
            self._lpm_exit(pin.id)

    def _halt_osc_up(self, pin: Any) -> None:
        self._lpm_ev = None
        if pin.level() != "L":                      # released before the oscillator was up: back to HALT
            self.emit("warn", block=self.name, msg=f"HALT wake-up on {pin.id} released before the oscillator "
                      "was stable: still in HALT")
            self._lpm_pin = None
            return
        if self.get("PLLSTS") & PLLOFF:
            self._lpm_exit(pin.id)
            return
        self.set("PLLSTS", self.get("PLLSTS") & ~1)
        t = float(self.pll_lock_ns) if self.pll_lock_ns is not None else PLL_LOCK_OSC_CYCLES * 1e9 / self.ext_hz
        self._lpm_ev = self.chip.sched.in_ns(t, self._halt_locked, pin)

    def _halt_locked(self, pin: Any) -> None:
        self._lpm_ev = None
        self.set("PLLSTS", self.get("PLLSTS") | 1)
        self._lpm_exit(pin.id)

    def _lpm_exit(self, source: str | None, wakeint: bool = True) -> None:
        mode = self.lpm
        if mode is None:
            return
        if self._lpm_ev is not None:
            self.chip.sched.cancel(self._lpm_ev)
            self._lpm_ev = None
        self._lpm_leave_state()
        for blk in self._clocked_blocks():
            blk.clock_stop(False, mode)
        if mode == "HALT":
            self._osc_changed()
        if source is not None:
            self.emit("info", block=self.name, msg=f"{mode} exit ({source})")
        if wakeint:
            self.pulse_irq("WAKEINT" if self._has_line("WAKEINT") else "1.8")
            if not self._wakeint_enabled():
                self.emit("warn", block=self.name, msg=f"{mode} exit: WAKEINT is not enabled (PIEIER1.8 / IER.INT1), "
                          "so the CPU stays in IDLE until another enabled interrupt")

    def _wakeint_enabled(self) -> bool:
        arch = self.chip.arch
        pie = getattr(arch, "pie", None)
        try:
            ier = self.chip.core.read_reg("ier")
        except Exception:
            return True
        return bool(ier & 1) and (pie is None or not pie.enpie or bool(pie.ier[1] & 0x80))

    # ------------------------------------------------------------------ PLL
    def _pllsts_write(self, blk, reg, old, new):
        new = (new & ~(1 | MCLKSTS)) | (old & (1 | MCLKSTS))      # PLLLOCKS, MCLKSTS read-only
        clr = bool(new & MCLKCLR)
        new &= ~MCLKCLR                           # reads 0
        if clr:                                   # clears MCLKSTS and resets the detector
            new &= ~MCLKSTS
        self.set("PLLSTS", new)
        if clr and self.limp:
            self.limp = False
            self.emit("info", block=self.name, msg="PLLSTS.MCLKCLR: missing-clock status cleared")
            self._apply_clock("MCLKCLR")
        if (old ^ new) & (3 << 7):
            self._apply_clock("DIVSEL")
        if clr or (old ^ new) & (OSCOFF | MCLKOFF):
            self._osc_changed()
        return new

    def _pllcr_write(self, blk, reg, old, new):
        div = new & 0xF
        if div > 10:
            self.emit("warn", block=self.name, msg=f"PLLCR.DIV={div} is reserved (max 10)")
        if self._divsel() not in (0, 1) and div != old & 0xF:
            self.emit("warn", block=self.name, msg="PLLCR written while PLLSTS.DIVSEL != 0 (TRM: set DIVSEL to /4 "
                      "before changing PLLCR)")
        if div == (old & 0xF) and self._lock_ev is None:
            return div
        if self.limp or not self.osc_fed():
            self.emit("warn", block=self.name, msg="PLLCR written without OSCCLK (limp mode): SPRUI07 1.3.2.5 says "
                      "the clocks stop")
            if not self.osc_fed():
                self.set("PLLCR", div)
                self._clock_dead("PLLCR written in limp mode (CLKIN switched to the missing OSCCLK/2)")
                return div
        self._locked_div = 0                      # bypass while locking
        self.set("PLLSTS", self.get("PLLSTS") & ~1)
        self._apply_clock("PLL relock (bypass)")
        if self._lock_ev is not None:
            self.chip.sched.cancel(self._lock_ev)
        t = float(self.pll_lock_ns) if self.pll_lock_ns is not None else PLL_LOCK_OSC_CYCLES * 1e9 / self.ext_hz
        self._lock_ev = self.chip.sched.in_ns(t, self._pll_locked, div)
        return div

    def _pll_locked(self, div: int) -> None:
        self._lock_ev = None
        self._locked_div = div
        self.set("PLLSTS", self.get("PLLSTS") | 1)
        self._apply_clock(f"PLL locked, DIV={div}")

    def _presc_write(self, blk, reg, old, new):
        return new & 7

    # ------------------------------------------------------------------ watchdog
    def wd_enabled(self) -> bool:
        return not self.get("WDCR") & 0x40

    def wd_period_ns(self) -> float:
        ps = self.get("WDCR") & 7
        presc = 1 if ps <= 1 else 1 << (ps - 1)
        return 512.0 * presc * 1e9 / self.ext_hz

    def _now(self) -> float:
        return self.chip.now_ns() if self.chip is not None else 0.0

    def _wd_running(self) -> bool:
        return self.wd_enabled() and not self._wd_held

    def _wd_hold(self, held: bool) -> None:
        """No OSCCLK (missing oscillator, HALT): the watchdog counter stops where it is."""
        if held == self._wd_held:
            return
        cnt = self.wd_count()
        self._wd_held = held
        self._wd_restart(cnt)

    def wd_count(self) -> int:
        if not self._wd_running():
            return self._wd_base_cnt
        dt = self._now() - self._wd_base_t
        return (self._wd_base_cnt + int(dt / self.wd_period_ns())) & 0xFF

    def _wd_restart(self, cnt: int) -> None:
        if self.chip is None:
            return
        if self._wd_ev is not None:
            self.chip.sched.cancel(self._wd_ev)
            self._wd_ev = None
        self._wd_base_cnt = cnt & 0xFF
        self._wd_base_t = self._now()
        if self._wd_running():
            t = (256 - self._wd_base_cnt) * self.wd_period_ns()
            self._wd_ev = self.chip.sched.in_ns(t, self._wd_overflow)

    def _wd_overflow(self) -> None:
        self._wd_ev = None
        if self.get("SCSR") & 2:                  # WDENINT: interrupt instead of reset
            self._wd_restart(0)
            self._wdint("watchdog timeout")
            return
        self._wd_reset("watchdog timeout")

    def _wdint(self, why: str) -> None:
        """WDINT goes low for 512 OSCCLK: WAKEINT on the falling edge, SCSR.WDINTS = 0 meanwhile."""
        if self._wdint_ev is not None:
            return                                # already low: no new falling edge
        self.set("SCSR", self.get("SCSR") & ~4)
        self._wdint_ev = self.chip.sched.in_ns(WDINT_OSC_CYCLES * 1e9 / self.ext_hz, self._wdint_end)
        self.pulse_irq("WAKEINT" if self._has_line("WAKEINT") else "1.8")
        if self.lpm == "STANDBY" and self._wd_wakes():
            self._lpm_exit("watchdog interrupt", wakeint=False)

    def _wdint_end(self) -> None:
        self._wdint_ev = None
        self.set("SCSR", self.get("SCSR") | 4)

    def _has_line(self, name: str) -> bool:
        irqs = getattr(getattr(self.chip, "desc", None), "irqs", None) or {}
        return name in irqs.values()

    def _wd_reset(self, why: str) -> None:
        self.emit("reset", block=self.name, reason="watchdog", msg=f"watchdog reset: {why}")
        self._wd_reset_flag = True
        arch = self.chip.arch
        arch.reset_requested = "watchdog"
        core = self.chip.core
        if getattr(core, "running", False):
            core.request_stop(from_mmio=True)

    def _wdcntr_read(self, blk, reg):
        return self.wd_count()

    def _wdkey_read(self, blk, reg):
        return self.get("WDCR") & 0xFF                 # SPRUI07 Table 1-29: WDKEY reads return WDCR

    def _wdkey_write(self, blk, reg, old, new):
        k = new & 0xFF
        if k == 0x55:
            self._wd_key55 = True
        elif k == 0xAA:
            if self._wd_key55:
                self._wd_restart(0)
            self._wd_key55 = False
        else:
            self._wd_key55 = False
        return 0

    def _wdcr_write(self, blk, reg, old, new):
        if (new >> 3) & 7 != 0b101:
            if self.get("SCSR") & 2:
                self.emit("warn", block=self.name, msg=f"WDCR written with WDCHK={(new >> 3) & 7:03b} (must be "
                          "101): watchdog interrupt")
                self._wdint("bad WDCHK")
            else:
                self.emit("warn", block=self.name, msg=f"WDCR written with WDCHK={(new >> 3) & 7:03b} (must be "
                          "101): immediate watchdog reset")
                self._wd_reset("bad WDCHK")
            return old
        val = new & 0x47                           # WDPS, WDDIS (WDCHK reads 0)
        if not self.get("SCSR") & 1:               # WDOVERRIDE cleared: WDDIS locked
            val = (val & ~0x40) | (old & 0x40)
        flag = old & 0x80
        if new & 0x80:                             # WDFLAG write-1-to-clear
            flag = 0
        cnt = self.wd_count()
        self.set("WDCR", val | flag)
        if (old ^ val) & 0x47:
            self._wd_restart(cnt)
            if (old ^ val) & 0x40:
                self.emit("info", block=self.name, msg="watchdog " + ("disabled" if val & 0x40 else "enabled"))
        return val | flag

    def _scsr_write(self, blk, reg, old, new):
        val = old & ~0x3
        if not (new & 1):                          # WDOVERRIDE: write 1 clears (w1c); 0 has no effect
            val |= old & 1
        val |= new & 2                             # WDENINT
        val = (val & ~4) | (old & 4)               # WDINTS read-only
        if old & 2 and not new & 2 and self._wdint_ev is not None:
            self.set("SCSR", val)
            self._wd_reset("SCSR.WDENINT cleared while WDINT is active")
        return val
