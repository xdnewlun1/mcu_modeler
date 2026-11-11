"""pwm_checker: a test-bench part that measures a PWM signal and complains when it is out of spec (template for
checkers / assertions in virtual time).

It timestamps every edge of its input with `self.now_ns` (the exact virtual time of the edge, instruction-precise for
a chip pin) and computes the period and duty cycle of each complete cycle. When the duty leaves [duty_min, duty_max]
or the frequency leaves [freq_min, freq_max] it logs one `warn` per excursion (and an info line when the signal is
back in range), counts violations and shows the live measurement in the UI. A checker like this turns a board run into
a timing test: `chipem run ... ` then look for `warn` events, or assert `state["violations"] == 0` in a pytest.

Board: boards/parts/stm32_pwm_check.yaml (STM32 TIM3 PWM, 1 kHz 25 %).
"""
from chipem.parts.script import ScriptPart, Param, on_rising, on_falling, action


class PwmChecker(ScriptPart):
    PORTS = {"IN": "in"}
    PARAMS = {
        "duty_min": Param(20.0, min=0, max=100, unit="%"),
        "duty_max": Param(30.0, min=0, max=100, unit="%"),
        "freq_min": Param(900.0, unit="Hz"),
        "freq_max": Param(1100.0, unit="Hz"),
    }

    def on_reset(self):
        self.t_rise = None              # time of the last rising edge (ns)
        self.t_fall = None
        self.bad = False                # currently out of spec?
        self.state.update(freq_hz=None, duty_pct=None, cycles=0, violations=0)

    @on_rising("IN")
    def rise(self):
        now = self.now_ns
        if self.t_rise is not None and self.t_fall is not None and self.t_fall > self.t_rise:
            period = now - self.t_rise
            high = self.t_fall - self.t_rise
            self.check(1e9 / period, 100.0 * high / period)
        self.t_rise = now

    @on_falling("IN")
    def fall(self):
        self.t_fall = self.now_ns

    def check(self, freq, duty):
        p = self.params
        self.state["cycles"] += 1
        self.state["freq_hz"] = round(freq, 2)
        self.state["duty_pct"] = round(duty, 3)
        ok = p["duty_min"] <= duty <= p["duty_max"] and p["freq_min"] <= freq <= p["freq_max"]
        if not ok and not self.bad:
            self.state["violations"] += 1
            self.warn(f"PWM out of spec: {freq:.1f} Hz, duty {duty:.2f} % (want {p['freq_min']:g}..{p['freq_max']:g} Hz, "
                      f"{p['duty_min']:g}..{p['duty_max']:g} %)")
        elif ok and self.bad:
            self.log(f"PWM back in spec: {freq:.1f} Hz, duty {duty:.2f} %")
        self.bad = not ok

    @action
    def clear(self):
        """Reset the counters (a button in the web UI)."""
        self.state.update(cycles=0, violations=0)
