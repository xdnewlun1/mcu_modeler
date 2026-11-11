"""ramp_source: an analog voltage that rises linearly from `v0` to `v1` between `t0` and `t0 + rise` and then holds
(a test source for comparator / ADC inputs).  The value goes to AOUT ("analog_out") every `update`; chip analog
inputs on its net (ADC channels, comparator inputs such as the Piccolo COMP1A) see it in volts.
State: `volts` (last value sent).
Board: boards/f28027_comp_adc.yaml (TMS320F28027 COMP1 tripping ePWM1 through digital compare).
"""
from chipem.parts.script import ScriptPart, Param


class RampSource(ScriptPart):
    PORTS = {"AOUT": "analog_out"}
    PARAMS = {
        "v0": Param(0.5, unit="V", doc="start voltage"),
        "v1": Param(2.5, unit="V", doc="end voltage"),
        "t0": Param("8ms", doc="ramp start"),
        "rise": Param("2ms", doc="ramp duration"),
        "update": Param("20us", doc="output refresh period"),
    }
    ADC_FULL_SCALE = 3.3

    def on_reset(self):
        self.t0 = self.ns(self.params["t0"])
        self.rise = max(1.0, float(self.ns(self.params["rise"])))
        self.state.update(volts=float(self.params["v0"]))
        self.analog("AOUT", float(self.params["v0"]))
        self.repeat(self.params["update"], self.refresh)

    def refresh(self):
        v0, v1 = float(self.params["v0"]), float(self.params["v1"])
        x = min(1.0, max(0.0, (self.now_ns - self.t0) / self.rise))
        v = round(v0 + (v1 - v0) * x, 4)
        if v != self.state["volts"]:
            self.state["volts"] = v
            self.analog("AOUT", v)
