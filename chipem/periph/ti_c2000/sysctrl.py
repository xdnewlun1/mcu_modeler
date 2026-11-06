"""C2833x/C2823x system control (0x7010): PLL and clock tree, peripheral clock gates, watchdog.

Clock tree (SPRS439 8.6, SPRUFB0 ch. 1):
  OSCCLK = params.ext_hz (crystal / XCLKIN, default 30 MHz)
  SYSCLKOUT = OSCCLK × PLLCR.DIV / {4 (DIVSEL 0/1), 2 (DIVSEL 2)}; with DIV = 0 (bypass) OSCCLK / {4, 2, 1 (DIVSEL 3)}
  HSPCLK = SYSCLKOUT / (2 × HISPCP) (HISPCP = 0 → /1);  LSPCLK = SYSCLKOUT / (2 × LOSPCP) (reset LOSPCP = 2 → /4)
After a PLLCR write the PLL relocks for 131072 OSCCLK cycles (PLLSTS.PLLLOCKS = 0, CPU in bypass); the new
SYSCLKOUT applies when PLLLOCKS returns to 1.  SYSCLKOUT drives `chip.clock` (the core clock) unless `frozen`
(the --clock override).  The boot ROM leaves DIVSEL = 2 (/2); we skip the boot ROM, so `params.boot_divsel`
(default 2) is applied after every reset.

Watchdog: WDCLK = OSCCLK / 512 / prescale (WDCR.WDPS 0,1 → /1, n → /2^(n-1)); 8-bit WDCNTR counts up and at 0xFF→0
resets the chip (`reset` event, `WDFLAG` set afterwards) or, with SCSR.WDENINT, pulses WAKEINT (PIE 1.8).  Writing
0x55 then 0xAA to WDKEY clears the counter.  WDCR writes must carry WDCHK = 101, otherwise the chip resets at once.
WDDIS can only change while SCSR.WDOVERRIDE = 1 (write 1 to clear it, locking WDDIS).  EALLOW protects every
register here.
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
            "WDKEY": {"on_write": self._wdkey_write, "on_read": lambda b, r: 0},
            "WDCR": {"on_write": self._wdcr_write},
        }
        regs = build_regs((name, "SYSCTRL"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, regs, params=params, log=log)
        self.ext_hz = float(params.get("ext_hz", 30_000_000))
        self.boot_divsel = int(params.get("boot_divsel", 2))
        self.pll_lock_ns = params.get("pll_lock_ns")
        self.frozen = False                 # set by the session for --clock
        self._lock_ev = None
        self._wd_ev = None
        self._wd_base_t = 0.0               # wall time at which WDCNTR was _wd_base_cnt
        self._wd_base_cnt = 0
        self._wd_key55 = False
        self._wd_reset_flag = False         # survives the watchdog reset → WDFLAG
        self._locked_div = 0                # PLLCR.DIV currently in effect (bypass while relocking)

    # ------------------------------------------------------------------ clocks
    def osc_hz(self) -> float:
        return self.ext_hz

    def _divsel(self) -> int:
        return (self.get("PLLSTS") >> 7) & 3

    def sysclk_hz(self) -> float:
        div = self._locked_div
        ds = self._divsel()
        osc = self.ext_hz
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

    def reset(self) -> None:
        if self.chip is not None:
            for ev in (self._lock_ev, self._wd_ev):
                if ev is not None:
                    self.chip.sched.cancel(ev)
        self._lock_ev = self._wd_ev = None
        super().reset()
        self._locked_div = 0
        st = self.get("PLLSTS") & ~(3 << 7)
        self.set("PLLSTS", st | ((self.boot_divsel & 3) << 7))
        self._wd_key55 = False
        if self._wd_reset_flag:
            self.set("WDCR", self.get("WDCR") | 0x80)
            self._wd_reset_flag = False
        self._apply_clock("reset")
        self._wd_restart(0)

    # ------------------------------------------------------------------ PLL
    def _pllsts_write(self, blk, reg, old, new):
        new = (new & ~1) | (old & 1)              # PLLLOCKS read-only
        if new & (1 << 4):                        # MCLKCLR: write 1 clears MCLKSTS, reads 0
            new &= ~((1 << 4) | (1 << 3))
        self.set("PLLSTS", new)
        if (old ^ new) & (3 << 7):
            self._apply_clock("DIVSEL")
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

    def wd_count(self) -> int:
        if not self.wd_enabled():
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
        if self.wd_enabled():
            t = (256 - self._wd_base_cnt) * self.wd_period_ns()
            self._wd_ev = self.chip.sched.in_ns(t, self._wd_overflow)

    def _wd_overflow(self) -> None:
        self._wd_ev = None
        if self.get("SCSR") & 2:                  # WDENINT: interrupt instead of reset
            self.set("SCSR", self.get("SCSR") & ~4)
            self.pulse_irq("WAKEINT" if self._has_line("WAKEINT") else "1.8")
            self.set("SCSR", self.get("SCSR") | 4)
            self._wd_restart(0)
            return
        self._wd_reset("watchdog timeout")

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
            self.emit("warn", block=self.name, msg=f"WDCR written with WDCHK={(new >> 3) & 7:03b} (must be 101): "
                      "immediate watchdog reset")
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
        return val
