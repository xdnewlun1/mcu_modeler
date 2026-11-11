"""P89CV51 programmable counter array (PCA): CCON D8H, CMOD D9H, CCAPM0-4 DAH-DEH, CL E9H, CH F9H, CCAPnL EAH-EEH,
CCAPnH FAH-FEH (data sheet sec. 6.9 pp.44-53, Tables 32-39).

Time base: the 16-bit CH:CL counter runs while CCON.CR = 1 (and not in idle with CMOD.CIDL = 1, not in power-down).
CMOD.CPS1:CPS0 = 00: once per peripheral clock (fosc/12, or fosc/6 in 6-clock mode with CKCON.PCAX2 = 0; the data
sheet's "fosc/6" assumes 6-clock mode), 01: three times as fast (fosc/4, fosc/2 in 6-clock mode; Table 34 prints
"fosc/6" twice, the text of sec. 6.9 says 1/2), 10: Timer 0 overflows, 11: falling edges on ECI (P1.2).
Rolling over FFFFH -> 0000H sets CF.  The count is computed lazily (exact to the oscillator period); events are
scheduled for what happens at a precise time: PWM edges, CEXn toggles (high-speed output), watchdog matches, and
the CF / CCFn flag-setting points while the flag is clear (flags always change from events, so a `JNB CCF0,$` wait
is skipped to the event).
Modules (CCAPMn = - ECOM CAPP CAPN MAT TOG PWM ECCF, Table 39):
* capture (CAPP / CAPN): a rising / falling edge on CEXn (P1.3-P1.7) copies CH:CL into CCAPnH:CCAPnL, sets CCFn;
* software timer (ECOM, MAT): CH:CL == CCAPnH:CCAPnL sets CCFn; writing CCAPnL clears ECOM, writing CCAPnH sets it
  (Fig. 22);
* high-speed output (ECOM, MAT, TOG): a match also toggles CEXn (PinBoard.drive_signal("CEXn"));
* PWM (ECOM, PWM): CEXn is LOW while CL < CCAPnL and HIGH when CL >= CCAPnL; CCAPnH is reloaded into CCAPnL when CL
  overflows FFH -> 00H (glitch-free update, sec. 6.9.4); 8-bit resolution, frequency = PCA clock / 256;
* watchdog (module 4, CMOD.WDTE = 1, ECOM + MAT): a match resets the device internally (`arch.request_reset(
  "pca_wdt")`; the RST pin is not driven, sec. 6.9.5).
Interrupt line "PCA" = CF & ECF | OR(CCFn & ECCFn).  CF and CCFn are cleared by software only.
Pins: modules in PWM or high-speed-output mode claim CEXn as an output (latch AND CEXn), capture modules claim it
as an input; the claim is released when the module mode changes.
"""
from __future__ import annotations

from typing import Any

from ..registry import register
from ._util import SfrBlock, find_model, listen_pin, periph_div, pin_level

CF, CR = 0x80, 0x40
CIDL, WDTE, ECF = 0x80, 0x40, 0x01
ECOM, CAPP, CAPN, MAT, TOG, PWM, ECCF = 0x40, 0x20, 0x10, 0x08, 0x04, 0x02, 0x01
CEX_PINS = ("P1.3", "P1.4", "P1.5", "P1.6", "P1.7")
ECI_PIN = "P1.2"


@register("mcs51.pca")
class Pca(SfrBlock):
    model = "mcs51.pca"
    table = ("PCA",)

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        ov = {"CCON": {"on_read": self._fold_r, "on_write": self._ccon_w, "ro_mask": 0x20, "on_peek": self._fold_peek},
              "CMOD": {"on_write": self._cfg_w, "ro_mask": 0x38},
              "CL": {"on_read": self._fold_r, "on_write": self._cnt_w, "on_peek": self._fold_peek},
              "CH": {"on_read": self._fold_r, "on_write": self._cnt_w, "on_peek": self._fold_peek}}
        for i in range(5):
            ov[f"CCAPM{i}"] = {"on_write": self._cfg_w, "ro_mask": 0x80}
            ov[f"CCAP{i}L"] = {"on_write": self._ccapl_w}
            ov[f"CCAP{i}H"] = {"on_write": self._ccaph_w}
        super().__init__(name, base, size, regs, params=params, log=log, overrides=ov)
        self.t0 = 0
        self._ev = None
        self._idle = False
        self._pd = False
        self.cex = ["L"] * 5
        self._pin_lvl = ["H"] * 5
        self._eci = "H"
        self._claimed: list[str | None] = [None] * 5
        self._t0_hooked = False
        self.matches = [0] * 5

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        for i, p in enumerate(CEX_PINS):
            listen_pin(chip, p, lambda lvl, _i=i: self._cex_changed(_i, lvl))
        listen_pin(chip, ECI_PIN, self._eci_changed)
        getattr(chip.arch, "ie_listeners", []).append(self._resched)

    def reset(self) -> None:
        self.cancel(self._ev)
        self._ev = None
        super().reset()
        self._idle = self._pd = False
        self.cex = ["L"] * 5
        self.matches = [0] * 5
        if self.chip is not None:
            self.t0 = self.now()
            self._pin_lvl = [pin_level(self.chip, p) for p in CEX_PINS]
            self._eci = pin_level(self.chip, ECI_PIN)
            for i in range(5):
                self._update_claim(i)
            self._hook_t0()
            self._line()

    # ------------------------------------------------------------------ state
    def cps(self) -> int:
        return self.get("CMOD") >> 1 & 3

    def running(self) -> bool:
        return bool(self.get("CCON") & CR) and not self._pd and not (self._idle and self.get("CMOD") & CIDL)

    def period(self) -> int | None:
        """Oscillator periods per count for the internal sources (None for T0 overflow / ECI)."""
        c = self.cps()
        d = periph_div(self.chip, "PCAX2")
        if c == 0:
            return d
        if c == 1:
            return max(d // 3, 1)
        return None

    def counter(self) -> int:
        return (self.get("CH") << 8) | self.get("CL")

    def _set_counter(self, v: int) -> None:
        self.set("CL", v & 0xFF)
        self.set("CH", (v >> 8) & 0xFF)

    def ccap(self, i: int) -> int:
        return (self.get(f"CCAP{i}H") << 8) | self.get(f"CCAP{i}L")

    def mode(self, i: int) -> int:
        return self.get(f"CCAPM{i}")

    def _is_pwm(self, i: int) -> bool:
        m = self.mode(i)
        return bool(m & ECOM and m & PWM)

    def _is_cmp(self, i: int) -> bool:
        m = self.mode(i)
        return bool(m & ECOM and m & MAT and not m & PWM)

    def _is_wdt(self, i: int) -> bool:
        return i == 4 and self._is_cmp(4) and bool(self.get("CMOD") & WDTE)

    # ------------------------------------------------------------------ counting
    def _advance(self, k: int) -> None:
        """k counts from the current value: CF, matches (flags, toggles, watchdog), PWM reloads."""
        if k <= 0:
            return
        v = self.counter()
        ccon = self.get("CCON")
        new_ccon = ccon
        if v + k > 0xFFFF:
            new_ccon |= CF
        for i in range(5):
            if self._is_cmp(i):
                t = self.ccap(i)
                d = (t - v - 1) & 0xFFFF            # counts until the counter equals t
                if d < k:
                    n = (k - d - 1) // 0x10000 + 1
                    self.matches[i] += n
                    new_ccon |= 1 << i
                    if self.mode(i) & TOG and n & 1:
                        self._set_cex(i, "H" if self.cex[i] == "L" else "L")
                    if self._is_wdt(i):
                        self._set_counter((v + k) & 0xFFFF)
                        self.set("CCON", new_ccon)
                        self.emit("info", block=self.name, msg="PCA watchdog match (module 4): internal reset")
                        self.chip.arch.request_reset("pca_wdt")
                        return
        cl = v & 0xFF
        wraps = (cl + k) >> 8
        if wraps:
            for i in range(5):
                if self._is_pwm(i):
                    self.set(f"CCAP{i}L", self.get(f"CCAP{i}H"))
        self._set_counter((v + k) & 0xFFFF)
        if new_ccon != ccon:
            self.set("CCON", new_ccon)
            self._line()
        self._pwm_levels()

    def _fold(self) -> None:
        if self.chip is None:
            return
        now = self.now()
        p = self.period()
        if not self.running() or p is None:
            self.t0 = now
            return
        k = (now - self.t0) // p
        if k <= 0:
            return
        self.t0 += k * p
        self._advance(k)

    def _pwm_levels(self) -> None:
        cl = self.get("CL")
        for i in range(5):
            if self._is_pwm(i):
                self._set_cex(i, "H" if cl >= self.get(f"CCAP{i}L") else "L")

    def _set_cex(self, i: int, lvl: str) -> None:
        if self.cex[i] == lvl:
            return
        self.cex[i] = lvl
        if self._claimed[i] == "out" and self.chip.pins is not None:
            self.chip.pins.drive_signal(f"CEX{i}", lvl)

    # ------------------------------------------------------------------ scheduling
    def _resched(self) -> None:
        if self.chip is None:
            return
        self.cancel(self._ev)
        self._ev = None
        p = self.period()
        if not self.running() or p is None:
            return
        v = self.counter()
        cl = v & 0xFF
        ccon = self.get("CCON")
        best = None

        def cand(d):
            nonlocal best
            if d > 0 and (best is None or d < best):
                best = d
        if not ccon & CF:
            cand(0x10000 - v)
        for i in range(5):
            m = self.mode(i)
            if self._is_cmp(i):
                need = m & TOG or self._is_wdt(i) or not ccon & (1 << i)
                if need:
                    cand(((self.ccap(i) - v - 1) & 0xFFFF) + 1)
            elif self._is_pwm(i):
                cand(0x100 - cl)                                  # CL overflow: reload + output low
                tl = self.get(f"CCAP{i}L")
                if tl > cl:
                    cand(tl - cl)                                 # output goes high
        if best is not None:
            self._ev = self.at(self.t0 + best * p, self._event)

    def _event(self) -> None:
        self._ev = None
        self._fold()
        self._resched()

    # ------------------------------------------------------------------ external count sources
    def _hook_t0(self) -> None:
        t01 = find_model(self.chip, "mcs51.timer01")
        if t01 is None:
            return
        want = self.cps() == 2
        lst = t01.ovf_listeners[0]
        if want and self._t0_ovf not in lst:
            lst.append(self._t0_ovf)
            t01._resched_all()
        elif not want and self._t0_ovf in lst:
            lst.remove(self._t0_ovf)
            t01._resched_all()

    def _t0_ovf(self) -> None:
        if self.running() and self.cps() == 2:
            self._advance(1)

    def _eci_changed(self, lvl: str) -> None:
        old, self._eci = self._eci, lvl
        if old != "L" and lvl == "L" and self.running() and self.cps() == 3:
            self._advance(1)

    def _cex_changed(self, i: int, lvl: str) -> None:
        old, self._pin_lvl[i] = self._pin_lvl[i], lvl
        m = self.mode(i)
        if not m & (CAPP | CAPN) or m & (MAT | TOG | PWM):
            return
        rise = old == "L" and lvl != "L"
        fall = old != "L" and lvl == "L"
        if (rise and m & CAPP) or (fall and m & CAPN):
            self._fold()
            v = self.counter()
            self.set(f"CCAP{i}L", v & 0xFF)
            self.set(f"CCAP{i}H", v >> 8)
            self.set("CCON", self.get("CCON") | (1 << i))
            self._line()

    # ------------------------------------------------------------------ pins / lines
    def _update_claim(self, i: int) -> None:
        ports = find_model(self.chip, "mcs51.port")
        if ports is None:
            return
        m = self.mode(i)
        pin, sig = CEX_PINS[i], f"CEX{i}"
        if self._is_pwm(i) or (self._is_cmp(i) and m & TOG):
            if self._claimed[i] != "out":
                if self._is_pwm(i):
                    self.cex[i] = "H" if self.get("CL") >= self.get(f"CCAP{i}L") else "L"
                ports.claim(pin, sig, self.cex[i])
                self._claimed[i] = "out"
                self.chip.pins.drive_signal(sig, self.cex[i])
        elif m & (CAPP | CAPN):
            if self._claimed[i] == "out":
                self.chip.pins.release_signal(sig)
            ports.claim(pin, sig, None)
            self._claimed[i] = "in"
        elif self._claimed[i] is not None:
            if self._claimed[i] == "out":
                self.chip.pins.release_signal(sig)
            ports.release(pin, sig)
            self._claimed[i] = None

    def _line(self) -> None:
        if self.chip is None:
            return
        ccon, cmod = self.get("CCON"), self.get("CMOD")
        on = bool(ccon & CF and cmod & ECF) or any(ccon >> i & 1 and self.mode(i) & ECCF for i in range(5))
        self.chip.arch.set_irq("PCA", on)

    # ------------------------------------------------------------------ SFR hooks
    def _fold_peek(self, blk, reg) -> int:
        """Debugger peek: CL/CH/CCON as a read (which folds the counter up to now) returns them, computed without the
        fold's side effects (match counts, CEXn toggles, PWM reloads, watchdog reset)."""
        p = self.period() if self.chip is not None else None
        if p is None or not self.running():
            return self.get(reg.name)
        k = (self.now() - self.t0) // p
        if k <= 0:
            return self.get(reg.name)
        v = self.counter()
        if reg.name == "CL":
            return (v + k) & 0xFF
        if reg.name == "CH":
            return ((v + k) >> 8) & 0xFF
        ccon = self.get("CCON")
        if v + k > 0xFFFF:
            ccon |= CF
        for i in range(5):
            if self._is_cmp(i) and ((self.ccap(i) - v - 1) & 0xFFFF) < k:
                ccon |= 1 << i
        return ccon

    def _fold_r(self, blk, reg):
        self._fold()
        return None

    def _ccon_w(self, blk, reg, old, new):
        self.set("CCON", old)
        self._fold()
        self.set("CCON", new)
        self.t0 = self.now()
        self._line()
        self._resched()
        return None

    def _cfg_w(self, blk, reg, old, new):
        self.set(reg.name, old)
        self._fold()
        self.set(reg.name, new)
        self.t0 = self.now()
        if reg.name.startswith("CCAPM"):
            i = int(reg.name[-1])
            self._update_claim(i)
            if self._is_pwm(i):
                self._pwm_levels()
        else:
            self._hook_t0()
        self._line()
        self._resched()
        return None

    def _cnt_w(self, blk, reg, old, new):
        self.set(reg.name, old)
        self._fold()
        self.set(reg.name, new)
        self.t0 = self.now()
        self._pwm_levels()
        self._resched()
        return None

    def _ccapl_w(self, blk, reg, old, new):
        i = int(reg.name[4])
        self.set(reg.name, old)
        self._fold()
        self.set(reg.name, new)
        m = self.mode(i)
        if m & MAT and not m & PWM and m & ECOM:
            self.set(f"CCAPM{i}", m & ~ECOM)            # Fig. 22: writing CCAPnL clears ECOM
        self._pwm_levels()
        self._resched()
        return None

    def _ccaph_w(self, blk, reg, old, new):
        i = int(reg.name[4])
        self.set(reg.name, old)
        self._fold()
        self.set(reg.name, new)
        m = self.mode(i)
        if m & MAT and not m & PWM:
            self.set(f"CCAPM{i}", m | ECOM)             # writing CCAPnH sets ECOM
        self._resched()
        return None

    # ------------------------------------------------------------------ clocks
    def clock_rebase(self) -> None:
        self._fold()

    def clock_resume(self) -> None:
        self.t0 = self.now()
        self._resched()

    def clock_stop(self, stopped: bool, mode: str) -> None:
        self._fold()
        if mode == "pd":
            self._pd = stopped
        elif mode == "idle":
            self._idle = stopped
        self.t0 = self.now()
        self._resched()
