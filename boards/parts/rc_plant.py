"""rc_plant: a first-order RC low-pass on a PWM pin, its capacitor voltage fed to an ADC input (a plant for closed-loop
control samples; template for analog plants driven by a digital output).

The capacitor charges toward `vhigh` while PWM is high and toward 0 V while it is low, with time constant `tau`
(R x C), evaluated exactly at every PWM edge (v(t) = target + (v0 - target) exp(-dt / tau)), so the result does not
depend on how fast the emulator runs.  Every `update` the voltage at that instant goes to AOUT ("analog_out": a chip
ADC input on its net converts it; ADC_FULL_SCALE = 3.0 V for the TI C2000 ADC, which takes raw codes).
State: `volts` (last value sent), `edges`.
Board: boards/f28335_fpu_loop.yaml (TMS320F28335 PI loop, firmware/samples_c2000_c/f28335_pid.c).
"""
import math

from chipem.parts.script import ScriptPart, Param


class RcPlant(ScriptPart):
    PORTS = {"PWM": "in", "AOUT": "analog_out"}
    PARAMS = {
        "tau": Param("1ms", doc="R x C"),
        "vhigh": Param(3.3, unit="V", doc="PWM high level"),
        "update": Param("10us", doc="ADC input refresh period"),
    }
    ADC_FULL_SCALE = 3.0

    def on_reset(self):
        self.v = 0.0
        self.t = self.now_ns
        self.high = False
        self.tau_ns = self.ns(self.params["tau"])
        self.state.update(volts=0.0, edges=0)
        self.analog("AOUT", 0.0)
        self.repeat(self.params["update"], self.refresh)

    def advance(self):
        now = self.now_ns
        target = float(self.params["vhigh"]) if self.high else 0.0
        self.v = target + (self.v - target) * math.exp(-(now - self.t) / self.tau_ns)
        self.t = now

    def on_change(self, port, level, t_ns):
        if port != "PWM":
            return
        self.advance()
        self.high = level == 1
        self.state["edges"] += 1

    def refresh(self):
        self.advance()
        self.state["volts"] = round(self.v, 4)
        self.analog("AOUT", self.v)
