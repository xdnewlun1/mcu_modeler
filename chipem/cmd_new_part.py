"""`chipem new-part NAME [--kind gpio|spi|i2c|uart]`: write a commented template for a scripted board part
(chipem/parts/script.py). Never overwrites a file."""
from __future__ import annotations

import re
import sys
from pathlib import Path

KINDS = ("gpio", "spi", "i2c", "uart")

_HEAD = '''"""{name}: a custom board part written in Python ({kind} template from `chipem new-part`).

Use it in a board YAML (the path is relative to the board file):

    parts:
      - {{id: U1, type: script, file: {file}, ports: {{{wiring}}}}}

Reference: docs/USER_GUIDE.md, "Writing your own part in Python". Examples: boards/parts/*.py.
Callbacks run on the emulation thread at the virtual time of the event; an exception stops this part (one `warn`
with the line number) until a reset or `part reload U1`. A reload re-reads this file without restarting the run.
"""
from chipem.parts.script import ScriptPart, Param, action, every, on_falling, on_rising  # noqa: F401


class {cls}(ScriptPart):
'''

_BODY = {
    "gpio": '''    # Ports: "in", "out", "inout", "od" (open drain; add " pullup"), "analog_in", "analog_out".
    PORTS = {"IN": "in", "OUT": "out"}
    # Settings: overridable from the board (params: {...}) and live from the web UI / shell (`part U1 delay=2ms`).
    PARAMS = {"delay": "1ms", "invert": False}

    def on_reset(self):
        """Power-on, every reset and every reload: initial outputs, timers, self.state (shown in the UI)."""
        self.drive("OUT", 0)
        self.state["edges"] = 0

    def on_change(self, port, level, t_ns):
        """Any input changed: level is 1, 0 or None (undriven). Called once after reset with the first level."""
        if port == "IN" and level is not None:
            self.state["edges"] += 1
            out = level ^ 1 if self.params["invert"] else level
            self.after(self.params["delay"], self.drive, "OUT", out)     # a delayed copy of IN

    @action
    def pulse(self):
        """A button in the web UI (`part U1 action=pulse` in the shell): a 100 us high pulse on OUT."""
        self.drive("OUT", 1)
        self.after("100us", self.drive, "OUT", 0)
''',
    "spi": '''    PORTS = {"CS": "in", "SCK": "in", "MOSI": "in", "MISO": "out"}
    PARAMS = {"mode": 0}

    def on_reset(self):
        # decodes CS/SCK/MOSI edges and shifts MISO; you get whole bytes
        self.spi_slave(cs="CS", sck="SCK", mosi="MOSI", miso="MISO", mode=self.params["mode"])
        self.regs = bytearray(16)
        self.cmd = None
        self.state["transfers"] = 0

    def on_spi_start(self):
        """CS asserted. Return the first byte(s) to send (sent while the master's first byte comes in)."""
        self.cmd = None
        return 0x00

    def on_spi_byte(self, value):
        """A byte from the master. Return the byte(s) to send next. Example protocol: [reg | 0x80 = read, reg] ..."""
        if self.cmd is None:
            self.cmd = value
            if value & 0x80:                       # read register value & 0x0F
                return self.regs[value & 0x0F]
            return None
        if not self.cmd & 0x80:                    # write register
            self.regs[self.cmd & 0x0F] = value
        return None

    def on_spi_end(self):
        self.state["transfers"] += 1
''',
    "i2c": '''    PORTS = {"SDA": "od pullup", "SCL": "od pullup"}
    PARAMS = {"address": Param(0x48, doc="7-bit address")}

    def on_reset(self):
        self.regs = bytearray(256)
        self.ptr = 0
        self.i2c_slave(self.params["address"], sda="SDA", scl="SCL")

    def on_i2c_write(self, data):
        """One write transfer: first byte = register pointer, then data (auto-increment)."""
        self.ptr = data[0]
        for b in data[1:]:
            self.regs[self.ptr] = b
            self.ptr = (self.ptr + 1) & 0xFF

    def on_i2c_read(self, n):
        """The master reads (n bytes already sent in this transfer). Return the next byte(s)."""
        b = self.regs[self.ptr]
        self.ptr = (self.ptr + 1) & 0xFF
        return b
''',
    "uart": '''    PORTS = {"RX": "in", "TX": "out"}
    # host: "" = the board wires; "pty" / "tcp:5555" = PC software talks to this device instead
    PARAMS = {"baud": 115200, "host": ""}

    def on_reset(self):
        self.uart(tx="TX", rx="RX", baud=self.params["baud"], host=self.params["host"] or None)
        self.state["lines"] = 0

    def on_uart_line(self, text):
        """A complete received line (LF ends it, CR is dropped). on_uart_byte(b) sees every byte."""
        self.state["lines"] += 1
        if text.strip().lower() == "ver?":
            self.uart_send("MYDEV 1.0\\r\\n")
        else:
            self.uart_send(f"ECHO {text}\\r\\n")

    @every("1s")
    def heartbeat(self):
        self.uart_send("ALIVE\\r\\n")
''',
}

_WIRING = {"gpio": "IN: A.PTC6, OUT: A.PTC5", "spi": "CS: A.PTC4, SCK: A.PTC5, MOSI: A.PTC6, MISO: A.PTC7",
           "i2c": "SDA: SDA, SCL: SCL", "uart": "RX: A.PTA2, TX: A.PTA1"}


def template(name: str, kind: str, file: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_]", "_", name)
    cls = "".join(w[:1].upper() + w[1:] for w in re.split(r"[_\-\s]+", stem) if w) or "MyPart"
    if cls[0].isdigit():
        cls = "Part" + cls
    return _HEAD.format(name=name, kind=kind, file=file, cls=cls, wiring=_WIRING[kind]) + _BODY[kind]


def add_args(p) -> None:
    p.add_argument("name", help="part name or path, e.g. my_sensor or boards/parts/my_sensor.py")
    p.add_argument("--kind", choices=KINDS, default="gpio", help="template: gpio (default), spi, i2c, uart")


def main(args) -> int:
    path = Path(args.name)
    if path.suffix != ".py":
        path = path.with_name(path.name + ".py")
    if path.exists():
        print(f"chipem new-part: {path} exists; not overwriting", file=sys.stderr)
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    text = template(path.stem, args.kind, path.name)
    with open(path, "x", encoding="utf-8") as f:            # "x": never replace a file created meanwhile
        f.write(text)
    print(f"wrote {path} ({args.kind} template, class {text.split('class ', 2)[-1].split('(')[0]})")
    print(f"use it in a board: - {{id: U1, type: script, file: {path.name}, ports: {{{_WIRING[args.kind]}}}}}")
    return 0
