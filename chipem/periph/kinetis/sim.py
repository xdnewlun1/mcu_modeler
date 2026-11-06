"""Kinetis SIM (System Integration Module): clock gates, SOPTx, CLKDIV1/2, SDID/FCFG/UID constants.

Register file from the SVD (offsets, widths, reset values). Behaviour:
* `clock_enabled(name)` / `check_gate(name)` decode the SCGC1-7 gate bits. Gated models (PORT, ADC, FTFE...)
  call `check_gate` on access; the first access to a gated-off peripheral logs one `warn` (real silicon
  raises a bus fault instead).
* CLKDIV1 (OUTDIV1 core, OUTDIV2 bus, OUTDIV3 FlexBus, OUTDIV4 flash) feeds the MCG model's clock tree:
  every write makes the MCG recompute the core clock.
"""
from __future__ import annotations

from typing import Any

from ..registry import register
from ._util import build_regs, find_model
from ..base import MMIOBlock

# gate name -> (register, bit)  (K22 RM chapter 12, MK22F12 SVD SCGCx fields)
GATES: dict[str, tuple[str, int]] = {
    "I2C2": ("SCGC1", 6), "UART4": ("SCGC1", 10), "UART5": ("SCGC1", 11),
    "DAC0": ("SCGC2", 12), "DAC1": ("SCGC2", 13),
    "SPI2": ("SCGC3", 12), "SDHC": ("SCGC3", 17), "FTM2": ("SCGC3", 24), "FTM3": ("SCGC3", 25),
    "ADC1": ("SCGC3", 27),
    "EWM": ("SCGC4", 1), "CMT": ("SCGC4", 2), "I2C0": ("SCGC4", 6), "I2C1": ("SCGC4", 7),
    "UART0": ("SCGC4", 10), "UART1": ("SCGC4", 11), "UART2": ("SCGC4", 12), "UART3": ("SCGC4", 13),
    "USBOTG": ("SCGC4", 18), "USB0": ("SCGC4", 18), "CMP": ("SCGC4", 19), "VREF": ("SCGC4", 20),
    "LPTMR": ("SCGC5", 0), "LPTMR0": ("SCGC5", 0),
    "PORTA": ("SCGC5", 9), "PORTB": ("SCGC5", 10), "PORTC": ("SCGC5", 11), "PORTD": ("SCGC5", 12),
    "PORTE": ("SCGC5", 13),
    "FTF": ("SCGC6", 0), "FTFE": ("SCGC6", 0), "DMAMUX": ("SCGC6", 1), "FLEXCAN0": ("SCGC6", 4),
    "CAN0": ("SCGC6", 4), "SPI0": ("SCGC6", 12), "SPI1": ("SCGC6", 13), "I2S": ("SCGC6", 15),
    "I2S0": ("SCGC6", 15), "CRC": ("SCGC6", 18), "USBDCD": ("SCGC6", 21), "PDB": ("SCGC6", 22),
    "PDB0": ("SCGC6", 22), "PIT": ("SCGC6", 23), "FTM0": ("SCGC6", 24), "FTM1": ("SCGC6", 25),
    "ADC0": ("SCGC6", 27), "RTC": ("SCGC6", 29),
    "FLEXBUS": ("SCGC7", 0), "FB": ("SCGC7", 0), "DMA": ("SCGC7", 1), "MPU": ("SCGC7", 2),
}


@register("kinetis.sim")
class SIM(MMIOBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        ov = {"CLKDIV1": {"on_write": self._clkdiv_write}}
        r = build_regs((name, "SIM"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self._warned: set[str] = set()
        self._mcg: Any = None

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._mcg = find_model(chip, "kinetis.mcg", "MCG")

    def reset(self) -> None:
        super().reset()
        self._warned.clear()            # (MCG.reset() re-derives the clocks, incl. CLKDIV1, after this)

    # ------------------------------------------------------------ gates
    def gate_location(self, name: str | None) -> tuple[int, int] | None:
        """(byte offset in self.mem, bit mask) of a gate, for fast checks by gated models."""
        g = GATES.get(name or "")
        if g is None:
            return None
        reg, bit = g
        return self.regs[reg].offset + bit // 8, 1 << (bit % 8)

    def clock_enabled(self, periph_name: str) -> bool:
        g = GATES.get(periph_name)
        if g is None:
            return True
        reg, bit = g
        return bool((self.get(reg) >> bit) & 1)

    def check_gate(self, name: str, block: str | None = None) -> bool:
        """True if `name`'s clock gate is on; otherwise logs a `warn` (once per peripheral) and returns False."""
        if self.clock_enabled(name):
            return True
        if name not in self._warned:
            self._warned.add(name)
            reg, bit = GATES[name]
            self.emit("warn", block=block or name, msg=f"{block or name} accessed while its clock gate is off "
                      f"(SIM_{reg} bit {bit}); real hardware would bus-fault")
        return False

    # ------------------------------------------------------------ clock dividers
    def outdiv(self, n: int) -> int:
        """OUTDIVn + 1 (divider of MCGOUTCLK) for n = 1 core/system, 2 bus, 3 FlexBus, 4 flash."""
        return ((self.get("CLKDIV1") >> (32 - 4 * n)) & 0xF) + 1

    def _clkdiv_write(self, blk, reg, old, new):
        if old != new and self._mcg is not None:
            self._mcg.update()
        return None
