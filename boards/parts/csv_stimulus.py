"""csv_stimulus: play a waveform from a CSV file into the board (template for stimulus generators).

Each CSV row is `time, volts[, level]` (time in ms from reset). The part schedules one timer per row, so every change
happens at its exact virtual time, independent of how fast the emulator runs:
  * AOUT ("analog_out") gets the voltage: chip ADC inputs on its net convert it (the STM32 ADC on PA0, a Kinetis
    ADC0_SE14 on PTC0, ...). With `interp: linear` the voltage ramps between rows in `resolution` steps.
  * DOUT ("out") gets the level column (0/1), if the file has one.
`repeat: true` loops the file (the last row's time is the period unless `period` is given).

Host-side helpers used here: `self.open(name)` opens a file relative to this script, `self.after(t, fn)` and
`self.repeat(t, fn)` schedule calls in virtual time (bare numbers are milliseconds, strings may say "250us").
Board: boards/parts/stm32_adc_csv.yaml.
"""
import csv

from chipem.parts.script import ScriptPart, Param


class CsvStimulus(ScriptPart):
    PORTS = {"AOUT": "analog_out", "DOUT": "out"}
    PARAMS = {
        "file": "waveform.csv",
        "interp": Param("step", choices=["step", "linear"]),
        "resolution": "100us",          # ramp step for interp: linear
        "repeat": False,
        "period": "",                   # loop period (default: the last row's time)
    }

    def on_reset(self):
        self.rows = []
        with self.open(self.params["file"]) as f:
            for rec in csv.reader(line for line in f if line.strip() and not line.lstrip().startswith("#")):
                t, v = float(rec[0]), float(rec[1])
                lvl = int(rec[2]) if len(rec) > 2 and rec[2].strip() else None
                self.rows.append((t, v, lvl))
        self.rows.sort()
        self.state["rows"] = len(self.rows)
        self.state["volts"] = None
        self.state["cycle"] = 0
        self.start_ms = self.now_ns / 1e6
        self.schedule_cycle()

    def schedule_cycle(self):
        for i, (t, v, lvl) in enumerate(self.rows):
            self.after(t, self.apply_row, i)            # relative to now: the start of this cycle
        if self.params["repeat"] and self.rows:
            period = self.params["period"] or self.rows[-1][0]
            self.after(period, self.next_cycle)

    def next_cycle(self):
        self.state["cycle"] += 1
        self.schedule_cycle()

    def apply_row(self, i):
        t, v, lvl = self.rows[i]
        self.set_volts(v)
        if lvl is not None:
            self.drive("DOUT", lvl)
        if self.params["interp"] == "linear" and i + 1 < len(self.rows):
            t2, v2, _ = self.rows[i + 1]
            step_ms = self.ns(self.params["resolution"]) / 1e6       # "100us" -> 0.1 (ms)
            n = int((t2 - t) / step_ms)
            for k in range(1, n):
                self.after(k * step_ms, self.set_volts, v + (v2 - v) * k / n)

    def set_volts(self, v):
        self.analog("AOUT", v)
        self.state["volts"] = round(v, 4)
