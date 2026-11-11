"""spi_world: a minimal SPI slave written in Python (template for SPI devices).

It answers every transaction with "WORLD", the reply the K22 `spi_master` sample expects from its peer, so it replaces
the STM32 running `spi_slave` in boards/k22_stm32_spi.yaml. Board: boards/parts/k22_spi_world.yaml.

How SPI works here: `spi_slave()` decodes the CS/SCK/MOSI net edges at their exact virtual time and shifts MISO for
you. You only see whole bytes:

  * on_spi_start()        CS went active. Return the first byte(s) to send (bytes, str, an int or a list).
  * on_spi_byte(value)    one byte arrived on MOSI. Return what to send *next* (SPI is full duplex: the byte that goes
                          out while byte n comes in had to be decided before byte n started).
  * on_spi_end()          CS went inactive (MISO is released: high impedance, like a real slave).

Anything you return is queued; when the queue runs dry the slave sends `fill` (0xFF unless you change it).
"""
from chipem.parts.script import ScriptPart


class SpiWorld(ScriptPart):
    # Port names are yours; the board YAML wires them to nets or chip pins.
    PORTS = {"CS": "in", "SCK": "in", "MOSI": "in", "MISO": "out"}
    # PARAMS are settings: the board can override them (params: {reply: ...}), and they can be changed at run time
    # from the web UI or the shell (`part U1 reply=HELLO`).
    PARAMS = {"reply": "WORLD"}

    def on_reset(self):
        # on_reset runs at power-on, on every reset and after a reload: set up helpers and the initial state here.
        self.spi_slave(cs="CS", sck="SCK", mosi="MOSI", miso="MISO", mode=0)
        self.rx = bytearray()
        self.state["transactions"] = 0      # self.state is shown in the web UI and by the shell `part` command
        self.state["last_rx"] = ""

    def on_spi_start(self):
        self.rx.clear()
        return self.params["reply"]         # the whole reply is queued: byte 0 goes out with the master's byte 0

    def on_spi_byte(self, value):
        self.rx.append(value)
        return None                         # nothing more to queue

    def on_spi_end(self):
        self.state["transactions"] += 1
        self.state["last_rx"] = self.rx.decode("latin-1")
        if self.state["transactions"] == 1:
            self.log(f"first transaction: received {self.state['last_rx']!r}")
