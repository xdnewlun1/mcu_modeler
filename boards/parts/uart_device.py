"""uart_device: a serial device that answers a line-based command protocol (template for UART peers).

Typical uses:
  * the peer of the MCU: a GPS, a modem, a sensor module, another board that the firmware talks to over a UART;
  * the peer of a PC tool: set the `host` parameter (e.g. "pty" or "tcp:5555") and the device talks to PC software
    instead of the board wires, so you can develop the PC side against an emulated device.

`uart()` gives you a UART on two ports at a baud rate. Bytes arrive at their stop bit time:
  * on_uart_byte(b)     every received byte (an int 0..255)
  * on_uart_line(text)  every complete line (ended by LF; CR is dropped), decoded as UTF-8
  * uart_send(data)     queue bytes (str is UTF-8); they leave one character time apart, like a real UART.
Bytes from the chip only arrive when the electrical path is sound (the board's serial path rules: polarity, levels,
a receiver listening), and baud/frame mismatches are reported like for any other part.

Protocol of this example (case-insensitive, one command per line):
  ping N         -> pong N          (the K22 `uart_link_sender` sample sends "ping N" every 2 ms)
  id?            -> CHIPEM-DEV 1.0
  temp?          -> TEMP 25.0       (the `temperature` parameter; change it live in the web UI)
  set temp X     -> OK
  anything else  -> ERR unknown command
"""
from chipem.parts.script import ScriptPart, Param, action


class UartDevice(ScriptPart):
    PORTS = {"RX": "in", "TX": "out"}
    PARAMS = {
        "baud": 115200,
        "temperature": Param(25.0, min=-40, max=125, step=0.5, unit="°C"),
        "host": "",                     # "" = the board wires; "pty", "pty:/tmp/ttyDEV", "tcp:5555" = PC software
    }

    def on_reset(self):
        self.uart(tx="TX", rx="RX", baud=self.params["baud"], host=self.params["host"] or None)
        self.state["commands"] = 0
        self.state["last"] = ""

    def on_uart_line(self, text):
        cmd = text.strip()
        if not cmd:
            return
        self.state["commands"] += 1
        self.state["last"] = cmd
        words = cmd.lower().split()
        if words[0] == "ping":
            reply = "pong " + " ".join(cmd.split()[1:])
        elif words == ["id?"]:
            reply = "CHIPEM-DEV 1.0"
        elif words == ["temp?"]:
            reply = f"TEMP {self.params['temperature']:.1f}"
        elif words[:2] == ["set", "temp"] and len(words) == 3:
            self.params["temperature"] = float(words[2])
            reply = "OK"
        else:
            reply = "ERR unknown command"
        self.uart_send(reply + "\r\n")

    @action
    def hello(self):
        """A button in the web UI (and `part ID action=hello` in the shell): send an unsolicited line."""
        self.uart_send("HELLO from the device\r\n")
