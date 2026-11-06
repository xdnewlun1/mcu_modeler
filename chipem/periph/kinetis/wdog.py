"""Kinetis WDOG (K22 RM ch. 24): 16-bit registers, unlock/refresh sequences, timeout -> chip reset.

* Unlock: 0xC520 then 0xD928 written consecutively to UNLOCK opens the write window (WCT, 256 bus clocks on
  silicon; here at least 4096 core cycles because fast-mode time is slice-granular). STCTRLH/L, TOVALH/L,
  WINH/L, PRESC and TMROUT writes outside the window (or after ALLOWUPDATE was cleared) are ignored
  with a `warn` (silicon would reset). Any configuration write restarts the counter.
* Refresh: 0xA602 then 0xB480 written consecutively to REFRESH restarts the counter (warn if WINEN and the
  counter is below WIN).
* Counter clock: CLKSRC=0 -> 1 kHz LPO, CLKSRC=1 -> alternate clock = bus clock (MCG/SIM derived), divided by
  PRESC.PRESCVAL+1. Timeout = TOVAL ticks. Scheduled with Scheduler.at_ns so it is independent of the core
  clock; a bus-clock change re-bases the running count.
  Reset defaults (SVD): STCTRLH 0x01D3 (WDOGEN=1, CLKSRC=1 -> bus clock), PRESC 0x0400 (PRESCVAL=4 -> /5),
  TOVAL 0x004C4B4C = 5,000,012 ticks; bus after reset = FEI 32,768 Hz x 640 = 20,971,520 Hz with
  SIM_CLKDIV1 OUTDIV2=0, so the reset timeout is 5,000,012 x 5 / 20,971,520 Hz = 1.192 s.
  (With CLKSRC=0 the same TOVAL would be 25,000 s on the LPO.)
* Timeout: `warn` + RSTCNT += 1 (RSTCNT is w1c and kept across all non-POR resets), then chip.reset("watchdog") (logs the
  `reset` event with reason "watchdog"; the RCM model reports SRS0.WDOG).
"""
from __future__ import annotations

from typing import Any

from ..base import MMIOBlock
from ..registry import register
from ._util import build_regs, find_model

LPO_HZ = 1000.0
UNLOCK_KEYS = (0xC520, 0xD928)
REFRESH_KEYS = (0xA602, 0xB480)
WDOGEN, CLKSRC, ALLOWUPDATE, WINEN = 1 << 0, 1 << 1, 1 << 4, 1 << 3
CONFIG_REGS = ("STCTRLH", "TOVALH", "TOVALL", "WINH", "WINL", "TMROUTH", "TMROUTL", "PRESC")


@register("kinetis.wdog")
class WDOG(MMIOBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        ov: dict[str, dict] = {n: {"on_write": self._cfg_write} for n in CONFIG_REGS}
        ov["STCTRLL"] = {"w1c_mask": 0x8000}                 # INTFLG: w1c at any time (not unlock-protected)
        ov["RSTCNT"] = {"w1c_mask": 0xFFFF}                  # w1c at any time; cleared only by POR
        ov["TMROUTH"]["on_read"] = self._tmrout_read
        ov["TMROUTL"]["on_read"] = self._tmrout_read
        ov["REFRESH"] = {"on_write": self._refresh_write}
        ov["UNLOCK"] = {"on_write": self._unlock_write}
        r = build_regs((name, "WDOG"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.unlock_window_cycles = int(params.get("unlock_window_cycles", 4096))
        self._ev: Any = None
        self._start_ns = 0.0
        self._tick_ns = 1e6
        self._unlock_stage = 0
        self._unlocked_until: int | None = None
        self._refresh_stage = 0
        self._update_locked = False
        self._powered = False
        self._warned_locked: set[str] = set()
        self._mcg: Any = None
        self._sim: Any = None
        self.timeouts = 0

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._mcg = find_model(chip, "kinetis.mcg", "MCG")
        self._sim = find_model(chip, "kinetis.sim", "SIM")
        if self._mcg is not None:
            self._mcg.listeners.append(self._clock_changed)

    def reset(self) -> None:
        self._cancel()
        keep = self.get("RSTCNT") if self._powered else 0     # RSTCNT survives every reset except POR
        self._powered = True
        super().reset()
        self.set("RSTCNT", keep)
        self._unlock_stage = 0
        self._unlocked_until = None
        self._refresh_stage = 0
        self._update_locked = False
        self._warned_locked.clear()
        self._restart(log=False)

    # ------------------------------------------------------------ derived values
    @property
    def enabled(self) -> bool:
        return bool(self.get("STCTRLH") & WDOGEN)

    @property
    def toval(self) -> int:
        return (self.get("TOVALH") << 16) | self.get("TOVALL")

    @property
    def win(self) -> int:
        return (self.get("WINH") << 16) | self.get("WINL")

    @property
    def prescaler(self) -> int:
        return ((self.get("PRESC") >> 8) & 7) + 1

    def source_hz(self) -> float:
        if not self.get("STCTRLH") & CLKSRC:
            return LPO_HZ
        if self._mcg is not None and self._mcg.chip is not None:
            return self._mcg.bus_hz_effective
        return self.chip.clock.hz / 2 if self.chip is not None else 10e6

    def tick_ns(self) -> float:
        return self.prescaler * 1e9 / self.source_hz()

    @property
    def timeout_s(self) -> float:
        return max(self.toval, 1) * self.tick_ns() / 1e9

    def counter(self) -> int:
        if self.chip is None or not self.enabled:
            return 0
        return max(0, int((self.chip.now_ns() - self._start_ns) / self._tick_ns))

    # ------------------------------------------------------------ scheduling
    def _cancel(self) -> None:
        if self._ev is not None and self.chip is not None:
            self.chip.sched.cancel(self._ev)
        self._ev = None

    def _schedule(self) -> None:
        self._cancel()
        if self.chip is None or not self.enabled:
            return
        self._ev = self.chip.sched.at_ns(self._start_ns + max(self.toval, 1) * self._tick_ns, self._timeout)

    def _restart(self, log: bool = False) -> None:
        if self.chip is None:
            return
        self._start_ns = self.chip.now_ns()
        self._tick_ns = self.tick_ns()
        self._schedule()
        if log:
            if self.enabled:
                src = "LPO 1 kHz" if not self.get("STCTRLH") & CLKSRC else f"bus {self.source_hz() / 1e6:.3f} MHz"
                self.emit("info", block=self.name, msg=f"WDOG timeout {self.timeout_s:.3f} s ({src}, prescaler "
                          f"{self.prescaler}, TOVAL={self.toval}); refresh 0xA602/0xB480 required",
                          timeout_s=self.timeout_s)
            else:
                self.emit("info", block=self.name, msg="WDOG disabled")

    def _clock_changed(self) -> None:
        if self.chip is None or not self.enabled or not self.get("STCTRLH") & CLKSRC:
            return
        n = self.counter()
        new_tick = self.tick_ns()
        if new_tick == self._tick_ns:
            return
        self._tick_ns = new_tick
        self._start_ns = self.chip.now_ns() - n * new_tick
        self._schedule()

    def _timeout(self) -> None:
        self._ev = None
        chip = self.chip
        self.timeouts += 1
        rst = (self.get("RSTCNT") + 1) & 0xFFFF
        self.set("RSTCNT", rst)
        self.emit("warn", block=self.name, msg=f"WDOG timeout after {self.timeout_s:.3f} s without refresh: "
                  f"watchdog reset (RSTCNT={rst})", rstcnt=rst)
        chip.reset("watchdog")

    # ------------------------------------------------------------ register hooks
    def _now_cycles(self) -> int:
        c = self.chip
        return c.clock.cycles + c.core.insns_in_slice()

    def _is_unlocked(self) -> bool:
        u = self._unlocked_until
        return u is not None and self.chip is not None and self._now_cycles() <= u

    def _cfg_write(self, blk, reg, old, new):
        if not self._is_unlocked() or self._update_locked:
            if reg.name not in self._warned_locked:
                self._warned_locked.add(reg.name)
                why = "ALLOWUPDATE=0" if self._update_locked else "WDOG is locked (unlock with 0xC520/0xD928)"
                self.emit("warn", block=self.name, msg=f"write 0x{new:04x} to WDOG_{reg.name} ignored: {why} "
                          "(silicon would reset)")
            return old
        if reg.name in ("TMROUTH", "TMROUTL"):
            # writing TMROUT resets the counter (RM: only in test mode); keep it simple
            self._restart()
            return 0
        if reg.name == "STCTRLH":
            if not new & ALLOWUPDATE:
                self._update_locked = True
        if new != old or reg.name == "STCTRLH":
            was = bool(old & WDOGEN) if reg.name == "STCTRLH" else self.enabled
            self._store(reg, new)
            now_en = self.enabled
            self._restart(log=(reg.name in ("STCTRLH", "TOVALL", "TOVALH", "PRESC") and (was or now_en)))
        return None

    def _tmrout_read(self, blk, reg):
        n = self.counter()
        return (n >> 16) & 0xFFFF if reg.name == "TMROUTH" else n & 0xFFFF

    def _unlock_write(self, blk, reg, old, new):
        if new == UNLOCK_KEYS[0]:
            self._unlock_stage = 1
        elif new == UNLOCK_KEYS[1] and self._unlock_stage == 1:
            self._unlock_stage = 0
            if self._update_locked:
                self.emit("warn", block=self.name, msg="WDOG unlock ignored: ALLOWUPDATE was cleared")
            else:
                self._unlocked_until = self._now_cycles() + self.unlock_window_cycles
        else:
            self._unlock_stage = 0
            self.emit("warn", block=self.name, msg=f"bad WDOG unlock sequence (0x{new:04x}); silicon would reset")
        return None

    def _refresh_write(self, blk, reg, old, new):
        if new == REFRESH_KEYS[0]:
            self._refresh_stage = 1
        elif new == REFRESH_KEYS[1] and self._refresh_stage == 1:
            self._refresh_stage = 0
            if self.enabled:
                if self.get("STCTRLH") & WINEN and self.counter() < self.win:
                    self.emit("warn", block=self.name, msg="WDOG refreshed before the window opened "
                              "(silicon would reset)")
                self._start_ns = self.chip.now_ns()
                self._tick_ns = self.tick_ns()
                self._schedule()
        else:
            self._refresh_stage = 0
        return None
