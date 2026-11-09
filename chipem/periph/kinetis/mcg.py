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
from ._util import build_regs, find_model, fmt_hz

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
        ov["S"] = {"ro_mask": 0x7F, "w1c_mask": 0x80}
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

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._sim = find_model(chip, "kinetis.sim", "SIM")
        if getattr(chip, "clock_override", None):
            self.frozen = True
        else:
            desc_hz = getattr(getattr(getattr(chip, "desc", None), "core", None), "clock_hz", None)
            if desc_hz and abs(chip.clock.hz - float(desc_hz)) > 0.5:
                self.frozen = True      # Chip(clock_hz=...) explicitly overrode the YAML clock

    def reset(self) -> None:
        super().reset()
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
        s |= self._f("C2", 0, 1) | (erefs << 1) | (clkst << 2) | (irefs << 4) | (plls << 5) | (plls << 6)
        self.set("S", s)

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


@register("kinetis.osc")
class OSC(MMIOBlock):
    """OSC0 control register (ERCLKEN, EREFSTEN, SCxP load capacitors): plain storage."""

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        r = build_regs((name, "OSC"), size, None, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
