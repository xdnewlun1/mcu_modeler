"""max31855: a MAX31855-like thermocouple-to-digital converter (template for read-only SPI sensors).

The real chip (Maxim MAX31855 data sheet, "Serial Interface" and Table 2 "Memory Map"):
  * SPI-compatible, read-only: CS low stops conversions and puts D31 on SO; each SCK falling edge shifts the next bit
    out (the master samples on the rising edge: SPI mode 0); 32 bits per read, then CS high starts a new conversion.
  * D31..D18  thermocouple temperature, 14-bit two's complement, 0.25 °C per LSB
  * D17       reserved (0)
  * D16       fault (1 when any of D2..D0 is set)
  * D15..D4   cold-junction (internal) temperature, 12-bit two's complement, 0.0625 °C per LSB
  * D3        reserved (0)
  * D2 SCV (short to VCC), D1 SCG (short to GND), D0 OC (open circuit)
  * a conversion takes up to 100 ms; this model returns the current setting at once.

Run-time controls (web UI number fields, or `part TC1 temperature=250.5` in the shell): temperature, internal and
fault ("none", "oc", "scg", "scv").
"""
from chipem.parts.script import ScriptPart, Param


def twos(value, bits):
    """A signed integer as a `bits`-wide two's complement field."""
    return value & ((1 << bits) - 1)


class Max31855(ScriptPart):
    PORTS = {"CS": "in", "SCK": "in", "SO": "out"}
    PARAMS = {
        "temperature": Param(25.0, min=-270, max=1800, step=0.25, unit="°C", doc="thermocouple (hot junction)"),
        "internal": Param(24.0, min=-55, max=125, step=0.0625, unit="°C", doc="cold junction"),
        "fault": Param("none", choices=["none", "oc", "scg", "scv"]),
    }

    def on_reset(self):
        # mosi=None: the MAX31855 has no data input
        self.spi_slave(cs="CS", sck="SCK", mosi=None, miso="SO", mode=0)
        self.state["reads"] = 0
        self.state["word"] = None

    def word(self):
        p = self.params
        fault_bits = {"none": 0, "oc": 0b001, "scg": 0b010, "scv": 0b100}[p["fault"]]
        tc = twos(round(p["temperature"] / 0.25), 14)
        cj = twos(round(p["internal"] / 0.0625), 12)
        w = (tc << 18) | ((1 if fault_bits else 0) << 16) | (cj << 4) | fault_bits
        return w

    def on_spi_start(self):
        # CS fell: latch the conversion result and queue its 4 bytes, MSB first
        w = self.word()
        self.state["reads"] += 1
        self.state["word"] = f"0x{w:08X}"
        return w.to_bytes(4, "big")
