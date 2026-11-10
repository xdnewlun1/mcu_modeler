"""Kinetis MCG (Multipurpose Clock Generator) and OSC models.

MCG status mirrors control immediately (no lock/settle delays), so SDK clock-init polling loops terminate:
IREFST = C1.IREFS, CLKST = FLL/PLL/IRC/EXT from C1.CLKS + C6.PLLS, OSCINIT0 = C2.EREFS0, IRCST = C2.IRCS,
PLLST = LOCK0 = C6.PLLS.

Clock tree (K22 RM ch. 25):
  FLL ref   = slow IRC 32.768 kHz (IREFS=1) or OSCCLK / FRDIV divider (/1..128 for RANGE0=0 or OSCSEL=RTC,
              /32..1536 otherwise)
  FLL out   = ref x {640,1280,1920,2560}[DRS] (DMX32=0) or {732,1464,2197,2929}[DRS] (DMX32=1)
              (reset FEI: 32768 x 640 = 20.97152 MHz)
  PLL out   = OSCCLK / (PRDIV0+1) x (VDIV0+24)
  IRC       = 32.768 kHz (IRCS=0) or 4 MHz / 2^FCRDIV (IRCS=1)
  MCGOUTCLK = FLL (CLKS=0, PLLS=0) | PLL (CLKS=0, PLLS=1) | IRC (CLKS=1) | OSCCLK (CLKS=2)
  core = MCGOUTCLK / (OUTDIV1+1), bus = MCGOUTCLK / (OUTDIV2+1)  (SIM_CLKDIV1)
OSCCLK frequency: params `ext_hz` (default 8 MHz); C7.OSCSEL=1 selects the 32.768 kHz RTC oscillator.

Clock monitors (25.4.4): `set_external_clock(False)` stops the OSC0 crystal (`set_rtc_clock(False)` the 32 kHz RTC
oscillator), e.g. from a test, a shell `py` call or a board script. OSCINIT0 then reads 0 and the PLL loses lock
(S.LOCK0 = 0, sticky S.LOLS0 set when the PLL was on). With C6.CME0 set a loss of OSC0 sets SC.LOCS0 and either resets
the chip (C2.LOCRE0, RCM SRS0.LOC) or raises the MCG interrupt; a loss of lock with C8.LOLRE resets (SRS0.LOL), else
C6.LOLIE0 raises the MCG interrupt; CME1/LOCRE1/LOCS1 (C8) do the same for the RTC oscillator. When MCGOUTCLK comes
from the stopped oscillator (FEE/FBE/PBE/PEE/BLPE, or C7.OSCSEL=1) the core has no clock: it halts (chip asleep, no
interrupt can wake it) until the oscillator runs again or a reset. The monitors are not filtered (the loss is seen at
once instead of after the floc detection time). Low-power exit (`stop_exit`): PEE -> PBE after VLPS/LLS (and
C5.PLLSTEN0 cleared) and after STOP with PLLSTEN0=0 (25.4.1 notes); the PLL relocks at once.

Whenever the derived core clock changes the model calls chip.clock.set_hz(core) unless `frozen` is set
(Session sets it for --clock; it is also set automatically when the Chip was constructed with an explicit
clock_hz different from the chip YAML's, or when chip.clock_override is truthy).  `core_hz`/`bus_hz` always
report the derived values; `bus_hz_effective` scales the bus clock to the clock the core actually runs at.
Other models subscribe to clock-tree changes with `mcg.listeners.append(cb)` (cb() without arguments).
"""
from __future__ import annotations

from typing import Any, Callable

from ..base import MMIOBlock
from ..registry import register
from ._util import build_regs, find_model, fmt_hz, irq_number

SLOW_IRC = 32768.0
FAST_IRC = 4_000_000.0
FLL_FACTORS = ((640, 1280, 1920, 2560), (732, 1464, 2197, 2929))
FRDIV_HIGH = (32, 64, 128, 256, 512, 1024, 1280, 1536)


@register("kinetis.mcg")
class MCG(MMIOBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        ctrl = {"on_write": self._ctrl_write}
        ov = {n: ctrl for n in ("C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "SC")}
        ov["S"] = {"ro_mask": 0x7F, "w1c_mask": 0x80, "on_write": self._flag_write}
        ov["SC"] = {"w1c_mask": 0x01, "on_write": self._ctrl_write}
        ov["C8"] = {"w1c_mask": 0x01, "ro_mask": 0x1E, "on_write": self._ctrl_write}
        r = build_regs((name, "MCG"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.ext_hz = float(params.get("ext_hz", 8_000_000))
        self.frozen = bool(params.get("frozen", False))
        self.listeners: list[Callable[[], None]] = []
        self.mode = "FEI"
        self.mcgout_hz = 0.0
        self.core_hz = 0.0
        self.bus_hz = 0.0
        self._sim: Any = None
        self._logged: tuple | None = None
        self._ff: float | None = None
        self.osc_running = bool(params.get("ext_running", True))
        self.rtc_osc_running = True
        self._irq: int | None = None
        self._line = False
        self._halted = False
        self._saved_gate = None

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._sim = find_model(chip, "kinetis.sim", "SIM")
        self._irq = irq_number(chip, "MCG")
        if getattr(chip, "clock_override", None):
            self.frozen = True
        else:
            desc_hz = getattr(getattr(getattr(chip, "desc", None), "core", None), "clock_hz", None)
            if desc_hz and abs(chip.clock.hz - float(desc_hz)) > 0.5:
                self.frozen = True      # Chip(clock_hz=...) explicitly overrode the YAML clock

    def reset(self) -> None:
        super().reset()
        if self._line:
            self._line = False
            if self._irq is not None and self.chip is not None:
                self.irq(self._irq, False)
        self.update()

    # ------------------------------------------------------------ helpers
    def _f(self, reg: str, lsb: int, width: int) -> int:
        return (self.get(reg) >> lsb) & ((1 << width) - 1)

    def _osc_hz(self) -> float:
        return SLOW_IRC if self._f("C7", 0, 1) else self.ext_hz

    def ffclk_hz(self) -> float:
        """MCGFFCLK, the fixed frequency clock: the FLL reference (slow IRC, or the FRDIV-divided external one)."""
        if self._f("C1", 2, 1):
            return SLOW_IRC
        frdiv = self._f("C1", 3, 3)
        if self._f("C2", 4, 2) == 0 or self._f("C7", 0, 1):
            div = 1 << frdiv
        else:
            div = FRDIV_HIGH[frdiv]
        return self._osc_hz() / div

    def fll_hz(self) -> float:
        return self.ffclk_hz() * FLL_FACTORS[self._f("C4", 7, 1)][self._f("C4", 5, 2)]

    def pll_hz(self) -> float:
        return self._osc_hz() / (self._f("C5", 0, 5) + 1) * (self._f("C6", 0, 5) + 24)

    def irc_hz(self) -> float:
        return SLOW_IRC if not self._f("C2", 0, 1) else FAST_IRC / (1 << self._f("SC", 1, 3))

    def _outdiv(self, n: int) -> int:
        if self._sim is not None:
            return self._sim.outdiv(n)
        return 1

    @property
    def bus_hz_effective(self) -> float:
        """Bus clock consistent with the clock the core actually runs at (honours --clock overrides)."""
        if self.chip is None:
            return self.bus_hz
        return self.chip.clock.hz * self._outdiv(1) / self._outdiv(2)

    # ------------------------------------------------------------ update
    def _ctrl_write(self, blk, reg, old, new):
        if old != new:
            # store first so update() sees the new value
            self._store(reg, new)
            self.update()
        return None

    def update(self) -> None:
        clks = self._f("C1", 6, 2)
        plls = self._f("C6", 6, 1)
        irefs = self._f("C1", 2, 1)
        erefs = self._f("C2", 2, 1)
        lp = self._f("C2", 1, 1)
        if clks == 0:
            clkst = 3 if plls else 0
        elif clks == 1:
            clkst = 1
        elif clks == 2:
            clkst = 2
        else:
            clkst = 3 if plls else 0
        s = self.get("S") & 0x80                                    # LOLS0 is a sticky w1c flag
        osc_ok = self._osc_ok()
        lock = plls if osc_ok else 0
        s |= self._f("C2", 0, 1) | ((erefs if self.osc_running else 0) << 1) | (clkst << 2) | (irefs << 4) \
            | (plls << 5) | (lock << 6)
        self.set("S", s)
        self._update_line()

        if clks == 0:
            if plls:
                out, mode = self.pll_hz(), "PEE"
            else:
                out, mode = self.fll_hz(), ("FEI" if irefs else "FEE")
        elif clks == 1:
            out = self.irc_hz()
            mode = "BLPI" if lp else "FBI"
        elif clks == 2:
            out = self._osc_hz()
            mode = "BLPE" if lp else ("PBE" if plls else "FBE")
        else:
            out, mode = self.fll_hz(), "FEI?"
        self.mode = mode
        self.mcgout_hz = out
        self.core_hz = out / self._outdiv(1)
        self.bus_hz = out / self._outdiv(2)
        key = (round(self.core_hz), round(self.bus_hz), mode)
        ff = self.ffclk_hz()
        if key == self._logged:
            if ff != self._ff:                  # only MCGFFCLK changed (FRDIV/IREFS): tell its users (FTM CLKS=10)
                self._ff = ff
                for cb in list(self.listeners):
                    cb()
            return
        self._logged = key
        self._ff = ff
        chip = self.chip
        if chip is not None and getattr(chip, "clock_override", None):
            self.frozen = True
        if chip is not None:
            if self.core_hz > 200e6:
                self.emit("warn", block=self.name, msg=f"MCG: core clock {fmt_hz(self.core_hz)} exceeds the "
                          f"120 MHz maximum ({mode})")
            msg = f"core clock {self.core_hz / 1e6:.3f} MHz ({mode}), bus {self.bus_hz / 1e6:.3f} MHz"
            if self.frozen:
                msg += f" (clock frozen at {chip.clock.hz / 1e6:.3f} MHz by override)"
            self.emit("info", block=self.name, msg=msg, core_hz=self.core_hz, bus_hz=self.bus_hz, mode=mode)
            if not self.frozen and self.core_hz > 0:
                chip.clock.set_hz(self.core_hz)
        for cb in list(self.listeners):
            cb()
        self._check_halt()

    # ------------------------------------------------------------ clock monitors / oscillators
    def _osc_ok(self) -> bool:
        """The selected external reference (OSC0, or the RTC oscillator with C7.OSCSEL=1) is running."""
        return self.rtc_osc_running if self._f("C7", 0, 1) else self.osc_running

    def uses_external(self) -> bool:
        return self.mode in ("FEE", "FBE", "PBE", "PEE", "BLPE")

    def monitors_enabled(self) -> bool:
        return bool(self._f("C6", 5, 1) or self._f("C8", 5, 1))

    def _flag_write(self, blk, reg, old, new):
        self._update_line()
        return None

    def _update_line(self) -> None:
        c8 = self.get("C8")
        lvl = bool((self.get("SC") & 1 and self._f("C6", 5, 1) and not self._f("C2", 7, 1))
                   or (self.get("S") & 0x80 and self._f("C6", 7, 1) and not c8 & 0x40)
                   or (c8 & 0x01 and c8 & 0x20 and not c8 & 0x80))
        if lvl != self._line:
            self._line = lvl
            if self._irq is not None and self.chip is not None:
                self.irq(self._irq, lvl)

    def set_external_clock(self, running: bool) -> None:
        """Start/stop the OSC0 crystal / external clock (EXTAL0). Stopping it is seen by the CME0 monitor and the PLL."""
        running = bool(running)
        if running == self.osc_running:
            return
        self.osc_running = running
        self.emit("info", block=self.name, msg=f"OSC0 external clock {'running' if running else 'stopped'}")
        if not running:
            pll_on = bool(self._f("C6", 6, 1) or self._f("C5", 6, 1)) and not self._f("C7", 0, 1)
            if pll_on:
                self.set("S", self.get("S") | 0x80)              # LOLS0 (sticky)
            if self._f("C6", 5, 1):                              # CME0
                self.set("SC", self.get("SC") | 0x01)            # LOCS0
                if self._f("C2", 7, 1) and self._reset("loc"):
                    return
            if pll_on and self._f("C8", 6, 1) and self._reset("lol"):
                return
        self.update()
        self._check_halt()

    def set_rtc_clock(self, running: bool) -> None:
        """Start/stop the 32.768 kHz RTC oscillator as seen by the MCG (CME1 monitor, OSCSEL=1 reference)."""
        running = bool(running)
        if running == self.rtc_osc_running:
            return
        self.rtc_osc_running = running
        if not running and self.get("C8") & 0x20:                 # CME1
            self.set("C8", self.get("C8") | 0x01)                 # LOCS1
            if self.get("C8") & 0x80 and self._reset("loc"):
                return
        self.update()
        self._check_halt()

    def _reset(self, why: str) -> bool:
        chip = self.chip
        if chip is None:
            return False
        self.emit("warn", block=self.name, msg=f"MCG {'loss of clock' if why == 'loc' else 'loss of lock'} reset")
        chip.arch.request_reset(why)
        return True

    def _check_halt(self) -> None:
        """No MCGOUTCLK (mode on a stopped oscillator): the core cannot run until the clock returns."""
        chip = self.chip
        if chip is None or not hasattr(chip, "wake_gate"):
            return
        dead = self.uses_external() and not self._osc_ok()
        if dead and not self._halted:
            self._halted = True
            self._saved_gate = chip.wake_gate
            chip.wake_gate = _no_wake
            chip.sleeping = True
            if chip.core.running:
                chip.core.request_stop(from_mmio=True)
            self.emit("warn", block=self.name, msg=f"MCGOUTCLK lost ({self.mode} on a stopped oscillator): core halted")
        elif not dead and self._halted:
            self._halted = False
            chip.wake_gate = self._saved_gate
            self._saved_gate = None
            chip.sleeping = True
            chip.wake()
            self.emit("info", block=self.name, msg="MCGOUTCLK back: core resumes")

    def stop_exit(self, mode: str) -> None:
        """Called by the SMC when the chip leaves STOP / VLPS / LLS (25.4.1 notes)."""
        if self.mode != "PEE":
            return
        c5 = self.get("C5")
        if mode in ("VLPS", "LLS") or not c5 & 0x20:
            if mode in ("VLPS", "LLS"):
                self.set("C5", c5 & ~0x20)
            self.set("C1", (self.get("C1") & 0x3F) | 0x80)     # CLKS = 10: PBE
            self.update()


def _no_wake() -> bool:
    return False


@register("kinetis.osc")
class OSC(MMIOBlock):
    """OSC0 control register (ERCLKEN, EREFSTEN, SCxP load capacitors): plain storage."""

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        r = build_regs((name, "OSC"), size, None, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
