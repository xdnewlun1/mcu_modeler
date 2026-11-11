"""Piccolo analog comparator with 10-bit DAC (type 0): `ti_c2000.comp` (COMP1 0x6400, COMP2 0x6420, COMP3 0x6440).

Source: SPRUI09A chapter 7 (2802x) = SPRUH18I chapter 9 (2806x); register offsets SPRUI09 Table 7-3, SPRS523P Table
9-23, SPRS698J (comparator registers).
* Inputs: COMPxA (non-inverting) is an analog pin; the inverting input is the COMPxB pin (COMPCTL.COMPSOURCE = 1) or
  the internal DAC (0): V = DACVAL x (VDDA - VSSA) / 1023 (7.3), or with DACCTL.DACSOURCE = 1 the ramp generator,
  RAMPSTS[15:6] as the DAC code.  Pin voltages come in through `set_input(pin, volts)` / `set_channel("COMP1A", V)`
  (the same path as the ADC: script parts' analog outputs, `--adc COMP1A=1.2V`); an AIO pin driven as a digital output
  is seen at 0 V / VDDA.
* Output (7.2, 7.6, Table 7-1): 1 when V(A) > V(B), 0 when V(B) > V(A); equal voltages keep the previous state
  (hysteresis; COMPHYSTCTL in the ADC block is storage).  CMPINV inverts asynchronously.  SYNCSEL = 0 passes the
  asynchronous output; SYNCSEL = 1 the output synchronised to SYSCLKOUT and qualified: a change passes after
  QUALSEL + 1 consecutive SYSCLKOUT samples (QUALSEL = 0: one-cycle synchroniser), shorter pulses are filtered.
  COMPDACEN = 0 powers the comparator down (output 0); the ADC bandgap (ADCCTL1.ADCBGPWD) must be on (7.5, one
  `warn` otherwise, the comparator still works).
* The output goes to the ePWM digital compare inputs (`out_listeners`, read as `out`) and to a GPIO muxed to COMPxOUT
  (PinBoard.drive_signal).  COMPSTS shows the qualified output and does not update while PCLKCR3.COMPxENCLK = 0.
* Ramp generator (7.4, `params.ramp`: present when the register table has RAMPSTS): with DACSOURCE = 1, RAMPSTS is
  loaded from RAMPMAXREF_SHDW when the ramp is enabled and stays static until the first PWMSYNC; every PWMSYNC copies
  the shadow registers to RAMPMAXREF_ACTIVE / RAMPDECVAL_ACTIVE and reloads RAMPSTS, which then decrements by
  RAMPDECVAL_ACTIVE every SYSCLKOUT down to 0 (then static).  A comparator trip (output 1 after CMPINV, while the ramp
  runs) reloads RAMPSTS from RAMPMAXREF_ACTIVE and holds it until the next PWMSYNC.  PWMSYNC n = DACCTL.RAMPSOURCE + 1
  is ePWMn's HRPCTL.PWMSYNCSEL pulse (CTR=PRD or CTR=ZERO, `ti_c2000.epwm_dc.pwmsync_listeners`).  The crossing of the
  ramp and the input is computed, not polled (one scheduler event per crossing).
"""
from __future__ import annotations

import re
from typing import Any

from ..registry import register
from ._util import C2000Block, build_regs, find_model

COMPDACEN, COMPSOURCE, CMPINV, SYNCSEL = 1 << 0, 1 << 1, 1 << 2, 1 << 8
DACSOURCE = 1 << 0


@register("ti_c2000.comp")
class Comp(C2000Block):
    adc_volts = True
    adc_volts_only = True
    eallow_regs = ("COMPCTL", "DACCTL")
    cycle_anchors = ("_ramp_c0",)

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        m = re.search(r"(\d+)$", name)
        self.index = int(params.get("index", m.group(1) if m else 1))
        ov = {"COMPCTL": {"on_write": self._cfg_write}, "COMPSTS": {"ro_mask": 0xFFFF, "on_read": self._sts_read},
              "DACCTL": {"on_write": self._dacctl_write}, "DACVAL": {"on_write": self._cfg_write},
              "RAMPMAXREF_ACTIVE": {"ro_mask": 0xFFFF}, "RAMPDECVAL_ACTIVE": {"ro_mask": 0xFFFF},
              "RAMPSTS": {"ro_mask": 0xFFFF, "on_read": self._rampsts_read}}
        regs = build_regs((name, f"COMP{self.index}", "COMP1"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, regs, params=params, log=log)
        self.gate = params.get("gate", f"COMP{self.index}ENCLK")
        self.vdda = float(params.get("vdda", 3.3))
        self.ramp = bool(params.get("ramp", "RAMPSTS" in self.regs))
        self.va = 0.0
        self.vb = 0.0
        self.raw = False                   # comparator output before CMPINV
        self.out = False                   # after CMPINV and qualification: ePWM DC / GPIO / COMPSTS
        self._sts = 0
        self._q_ev = None
        self._x_ev = None                  # ramp crossing / end event
        self.out_listeners: list = []      # cb(index, level) when `out` changes
        self.transitions = 0
        self._ramp_on = False
        self._ramp_run = False             # decrementing (after a PWMSYNC, until 0 or a trip)
        self._ramp_c0 = 0                  # cycle at which RAMPSTS was _ramp_v0
        self._ramp_v0 = 0
        self._ramp_src = None
        self._bg_warned = False
        self._adc = None

    # ------------------------------------------------------------------ lifecycle
    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._adc = find_model(chip, "ti_c2000.adc_soc")
        self._gpio = find_model(chip, "ti_c2000.gpio")

    def reset(self) -> None:
        if self.chip is not None:
            for ev in (self._q_ev, self._x_ev):
                if ev is not None:
                    self.chip.sched.cancel(ev)
        self._q_ev = self._x_ev = None
        self._ramp_unsub()
        super().reset()
        self.raw = self.out = False
        self._sts = 0
        self._ramp_on = self._ramp_run = False
        self._ramp_v0 = 0
        if self.chip is not None and self.chip.pins is not None:
            self.chip.pins.release_signal(f"COMP{self.index}OUT")

    # ------------------------------------------------------------------ inputs
    def _side(self, name: str) -> str | None:
        n = str(name).strip().upper()
        if n in (f"COMP{self.index}A", "A"):
            return "A"
        if n in (f"COMP{self.index}B", "B"):
            return "B"
        return None

    def set_channel(self, name: str, value) -> bool:
        if not isinstance(value, float):
            raise ValueError(f"{self.name} input {name}: give volts (e.g. 1.2V), not a raw code {value!r}")
        return self.set_input(name, value)

    def set_input(self, name: str, volts) -> bool:
        if not isinstance(volts, float):
            return False
        parts = [str(name).strip().upper()]
        pins = getattr(self.chip, "pins", None) if self.chip is not None else None
        if pins is not None:
            try:
                parts = [s.strip().upper() for s in pins.get(name).name.split("/")]
            except KeyError:
                pass
        hit = False
        for p in parts:
            s = self._side(p) if p.startswith("COMP") else None
            if s == "A":
                self.va = float(volts)
                hit = True
            elif s == "B":
                self.vb = float(volts)
                hit = True
        if hit:
            self._eval()
        return hit

    def _pin_override(self, side: str) -> float | None:
        """An AIO pin of this input driven as a digital output: 0 V / VDDA."""
        f = getattr(self._gpio, "aio_drive", None) if getattr(self, "_gpio", None) is not None else None
        if f is None:
            return None
        lvl = f(f"COMP{self.index}{side}")
        return None if lvl is None else (self.vdda if lvl == "H" else 0.0)

    def dac_volts(self) -> float:
        if self.get("DACCTL") & DACSOURCE and self.ramp:
            code = (self.ramp_value() >> 6) & 0x3FF
        else:
            code = self.get("DACVAL") & 0x3FF
        return code * self.vdda / 1023.0

    def vin(self) -> tuple[float, float]:
        a = self._pin_override("A")
        a = self.va if a is None else a
        if self.get("COMPCTL") & COMPSOURCE:
            b = self._pin_override("B")
            b = self.vb if b is None else b
        else:
            b = self.dac_volts()
        return a, b

    # ------------------------------------------------------------------ evaluation
    def _eval(self) -> None:
        if self.chip is None:
            return
        ctl = self.get("COMPCTL")
        if not ctl & COMPDACEN:
            raw = False
        else:
            a, b = self.vin()
            raw = True if a > b else (False if b > a else self.raw)
            if not self._bg_warned and self._adc is not None and not self._adc.get("ADCCTL1") & (1 << 6):
                self._bg_warned = True
                self.emit("warn", block=self.name, msg=f"{self.name} enabled with the ADC bandgap off "
                          "(ADCCTL1.ADCBGPWD = 0, SPRUI09 7.5)")
        self.raw = raw
        want = raw ^ bool(ctl & CMPINV) if ctl & COMPDACEN else False
        if ctl & SYNCSEL:
            if want == self.out:
                if self._q_ev is not None:
                    self.chip.sched.cancel(self._q_ev)       # glitch shorter than the qualification
                    self._q_ev = None
            elif self._q_ev is None:
                q = ((ctl >> 3) & 0x1F) + 1
                self._q_ev = self.chip.sched.in_cycles(q, self._qualified, want)
        else:
            if self._q_ev is not None:
                self.chip.sched.cancel(self._q_ev)
                self._q_ev = None
            self._set_out(want)
        self._ramp_schedule()

    def _qualified(self, want: bool) -> None:
        self._q_ev = None
        self._set_out(want)

    def _set_out(self, lvl: bool) -> None:
        if lvl == self.out:
            return
        self.out = lvl
        self.transitions += 1
        sc = self._sysctrl
        if sc is None or sc.clock_enabled(self.gate):
            self._sts = int(lvl)
        pins = getattr(self.chip, "pins", None)
        if pins is not None:
            pins.drive_signal(f"COMP{self.index}OUT", "H" if lvl else "L")
        for cb in list(self.out_listeners):
            cb(self.index, lvl)
        if lvl and self._ramp_run:                    # trip: RAMPSTS back to RAMPMAXREF_ACTIVE, static until PWMSYNC
            self._ramp_load(self.get("RAMPMAXREF_ACTIVE"), run=False)

    # ------------------------------------------------------------------ ramp generator
    def ramp_value(self) -> int:
        if not self._ramp_run or self.chip is None:
            return self._ramp_v0
        dec = self.get("RAMPDECVAL_ACTIVE")
        n = self.chip.now_cycles() - self._ramp_c0
        return max(0, self._ramp_v0 - dec * n)

    def _ramp_load(self, v: int, run: bool) -> None:
        self._ramp_v0 = v & 0xFFFF
        self._ramp_c0 = self.chip.now_cycles() if self.chip is not None else 0
        self._ramp_run = run
        if self.chip is not None:
            self._eval()

    def _ramp_schedule(self) -> None:
        """Schedule the cycle at which the falling ramp crosses V(A) (or reaches 0)."""
        if self.chip is None:
            return
        if self._x_ev is not None:
            self.chip.sched.cancel(self._x_ev)
            self._x_ev = None
        if not (self._ramp_run and self.get("DACCTL") & DACSOURCE and not self.get("COMPCTL") & COMPSOURCE):
            return
        dec = self.get("RAMPDECVAL_ACTIVE")
        v = self.ramp_value()
        if not dec or v == 0:
            return
        a = self._pin_override("A")
        a = self.va if a is None else a
        # first ramp value whose DAC voltage is below V(A): code = v >> 6 with code * vdda / 1023 < a
        code_lt = int(a * 1023 / self.vdda)               # DAC codes <= this are <= a
        if code_lt * self.vdda / 1023.0 >= a:
            code_lt -= 1
        target = (code_lt + 1) << 6 if code_lt >= 0 else 0  # RAMPSTS below `target` gives a DAC code <= code_lt
        if (v >> 6) <= code_lt:
            target = 0                                     # already below: next change is the end of the ramp
        need = v - target + 1 if target else v
        n = max(1, -(-need // dec))
        self._x_ev = self.chip.sched.in_cycles(n, self._ramp_tick)

    def _ramp_tick(self) -> None:
        self._x_ev = None
        if self.ramp_value() == 0 and self._ramp_run:
            self._ramp_v0, self._ramp_run = 0, False       # static at zero until the next PWMSYNC
        self._eval()

    def _pwmsync(self, index: int) -> None:
        if not (self.get("DACCTL") & DACSOURCE and self.ramp):
            return
        self.set("RAMPMAXREF_ACTIVE", self.get("RAMPMAXREF_SHDW"))
        self.set("RAMPDECVAL_ACTIVE", self.get("RAMPDECVAL_SHDW"))
        self._ramp_load(self.get("RAMPMAXREF_ACTIVE"), run=True)

    def _ramp_unsub(self) -> None:
        src = self._ramp_src
        if src is not None and self._pwmsync in src.pwmsync_listeners:
            src.pwmsync_listeners.remove(self._pwmsync)
            if src.running:
                src._reschedule(first=False)
        self._ramp_src = None

    def _ramp_sub(self) -> None:
        n = ((self.get("DACCTL") >> 1) & 0xF) + 1
        src = self.chip.peripherals.get(f"EPWM{n}") if self.chip is not None else None
        if src is self._ramp_src:
            return
        self._ramp_unsub()
        if src is not None and hasattr(src, "pwmsync_listeners"):
            src.pwmsync_listeners.append(self._pwmsync)
            self._ramp_src = src
            if src.running:
                src._reschedule(first=False)

    # ------------------------------------------------------------------ registers
    def _cfg_write(self, blk, reg, old, new):
        self._store(reg, new)
        self._eval()
        return None

    def _dacctl_write(self, blk, reg, old, new):
        self._store(reg, new)
        if self.ramp and new & DACSOURCE:
            if not old & DACSOURCE:
                self._ramp_load(self.get("RAMPMAXREF_SHDW"), run=False)   # static until the first PWMSYNC
            self._ramp_sub()
        else:
            self._ramp_unsub()
            self._ramp_run = False
        self._eval()
        return None

    def _sts_read(self, blk, reg):
        return self._sts

    _sts_read.pure_read = True        # type: ignore[attr-defined]

    def _rampsts_read(self, blk, reg):
        return self.ramp_value()

    _rampsts_read.pure_read = True    # type: ignore[attr-defined]
