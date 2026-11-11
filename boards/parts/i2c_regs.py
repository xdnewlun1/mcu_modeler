"""i2c_regs: an I2C register-map device in ~20 lines (template for I2C sensors, expanders, EEPROM-like parts).

Convention used by most I2C sensors: the first byte of a write transfer is the register pointer, further bytes are
written to consecutive registers; a read transfer returns registers from the pointer on. The pointer auto-increments.

`i2c_slave(address)` decodes START/STOP/address/data from the SDA/SCL net edges, acknowledges its address and drives
SDA (open drain) at the right SCL edges. You see whole transfers:

  * on_i2c_write(data)  the bytes of one write transfer (called at its STOP or repeated START)
  * on_i2c_read(n)      the master reads; n = bytes already sent in this transfer. Return the byte(s) to send;
                        you are called again if the master wants more.

Board: boards/parts/k22_i2c_regs.yaml (the K22 `i2c_reg_master` sample writes "Hello over I2C!" at register 0x10,
reads it back and prints PASS).
"""
from chipem.parts.script import ScriptPart, Param


class I2cRegs(ScriptPart):
    # "od pullup": open-drain port with the breakout board's pull-up resistor on the line.
    PORTS = {"SDA": "od pullup", "SCL": "od pullup"}
    PARAMS = {"address": Param(0x42, doc="7-bit I2C address")}

    def on_reset(self):
        self.regs = bytearray(256)
        self.ptr = 0
        self.i2c_slave(self.params["address"], sda="SDA", scl="SCL")

    def on_i2c_write(self, data):
        self.ptr = data[0]
        for b in data[1:]:
            self.regs[self.ptr] = b
            self.ptr = (self.ptr + 1) & 0xFF
        self.state["ptr"] = self.ptr

    def on_i2c_read(self, n):
        b = self.regs[self.ptr]
        self.ptr = (self.ptr + 1) & 0xFF
        return b
