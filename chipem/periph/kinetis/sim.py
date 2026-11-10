"""Kinetis SIM (System Integration Module): clock gates, SOPTx, CLKDIV1/2, SDID/FCFG/UID constants.

Register file from the SVD (offsets, widths, reset values). Behaviour:
* `clock_enabled(name)` / `check_gate(name)` decode the SCGC1-7 gate bits. Gated models (PORT, ADC, FTFE...)
  call `check_gate` on access; the first access to a gated-off peripheral logs one `warn` (real silicon
  raises a bus fault instead).
* CLKDIV1 (OUTDIV1 core, OUTDIV2 bus, OUTDIV3 FlexBus, OUTDIV4 flash) feeds the MCG model's clock tree:
  every write makes the MCG recompute the core clock.
* SOPT7 (12.2.6) selects the ADC0/ADC1 hardware trigger (ADCnALTTRGEN, ADCnPRETRGSEL, ADCnTRGSEL); the ADC models
  read it when a trigger arrives (kinetis.adc `alt_trigger`). All sources 0-14 are wired
  (`subscribe_trigger_sources`, shared with PDB0 TRGSEL); selecting the reserved value 15, or a source whose model
  is not on the chip, logs one `info` per ADC and source.
"""
from __future__ import annotations

from typing import Any

from ...pins import PinBoard
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
# second gate bit of a peripheral: its clock runs when either bit is set (RM 12.2.10/12.2.13: FTM2 is gated by
# SIM_SCGC3[FTM2] (bit 24) and by SIM_SCGC6[FTM2] (bit 26))
GATES_ALT: dict[str, tuple[str, int]] = {"FTM2": ("SCGC6", 26)}

# SIM_SOPT7 ADCnTRGSEL encodings (12.2.6) and which of them reach the ADC model
ADC_TRGSEL = {0: "PDB0_EXTRG pin", 1: "CMP0 output", 2: "CMP1 output", 3: "CMP2 output", 4: "PIT trigger 0",
              5: "PIT trigger 1", 6: "PIT trigger 2", 7: "PIT trigger 3", 8: "FTM0 trigger", 9: "FTM1 trigger",
              10: "FTM2 trigger", 11: "FTM3 trigger", 12: "RTC alarm", 13: "RTC seconds", 14: "LPTMR trigger",
              15: "reserved"}


def subscribe_trigger_sources(chip: Any, fire: Any = None) -> set[int]:
    """Wire the K22 trigger sources to `fire(sel)`, `sel` in the encoding shared by PDB0 SC.TRGSEL (Table 3-51) and
    SIM_SOPT7 ADCnTRGSEL (12.2.6): 0 rising edge on a pin muxed to PDB0_EXTRG, 1..3 rising edge of CMP0..CMP2 COUT,
    4..7 PIT channel 0..3 timeout, 8..11 FTM0..FTM3 trigger output (EXTTRIG), 12 RTC alarm, 13 RTC seconds,
    14 LPTMR0 compare. The subscriber checks its own selection (cheap; the sources fire at their event rate).
    Returns the selections that have a source on this chip; `fire=None` only computes that set."""
    periphs = chip.peripherals
    wired: set[int] = set()

    def hook(sel: int, lst: Any, cb: Any) -> None:
        if lst is not None:
            wired.add(sel)
            if fire is not None and cb is not None:
                lst.append(cb)

    for i in range(4):
        hook(8 + i, getattr(periphs.get(f"FTM{i}"), "trigger_listeners", None), lambda ftm, _s=8 + i: fire(_s))
    for i in range(3):
        hook(1 + i, getattr(periphs.get(f"CMP{i}"), "cout_listeners", None),
             lambda cmp, level, _s=1 + i: fire(_s) if level else None)
    pit = getattr(periphs.get("PIT"), "trigger_listeners", None)
    for n in range(4):
        hook(4 + n, pit, None)
    if pit is not None and fire is not None:
        pit.append(lambda p, n: fire(4 + n) if n < 4 else None)
    rtc = getattr(periphs.get("RTC"), "trigger_listeners", None)
    hook(12, rtc, None)
    hook(13, rtc, lambda r, kind: fire(12 if kind == "alarm" else 13))
    hook(14, getattr(periphs.get("LPTMR0"), "trigger_listeners", None), lambda lptmr: fire(14))
    pins = getattr(chip, "pins", None)
    if pins is not None and pins.signal_pins("PDB0_EXTRG"):
        wired.add(0)
    if pins is not None and fire is not None:
        last: dict[str, int] = {}

        def extrg(pin: Any) -> None:
            lvl = 1 if pin.level() == "H" else 0
            was = last.get(pin.id, 0)
            last[pin.id] = lvl
            if lvl and not was and pin.direction == "in" and PinBoard.signal_matches(pin.owner, "PDB0_EXTRG"):
                fire(0)
        for p in pins.signal_pins("PDB0_EXTRG"):
            last[p.id] = 1 if p.level() == "H" else 0
            pins.input_listeners[p.id].append(extrg)
    return wired


@register("kinetis.sim")
class SIM(MMIOBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        ov = {"CLKDIV1": {"on_write": self._clkdiv_write}, "SOPT7": {"on_write": self._sopt7_write}}
        r = build_regs((name, "SIM"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self._warned: set[str] = set()
        self._trg_noted: set[tuple[int, int]] = set()
        self._mcg: Any = None

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._mcg = find_model(chip, "kinetis.mcg", "MCG")

    def reset(self) -> None:
        super().reset()
        self._trg_noted: set[tuple[int, int]] = set()
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
        if (self.get(reg) >> bit) & 1:
            return True
        alt = GATES_ALT.get(periph_name)
        return alt is not None and bool((self.get(alt[0]) >> alt[1]) & 1)

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

    # ------------------------------------------------------------ ADC trigger select
    def adc_trigger_select(self, n: int) -> tuple[bool, int, int]:
        """(ALTTRGEN, PRETRGSEL, TRGSEL) of ADCn from SOPT7."""
        b = (self.get("SOPT7") >> (8 * n)) & 0xFF
        return bool(b & 0x80), (b >> 4) & 1, b & 0x0F

    def _sopt7_write(self, blk, reg, old, new):
        wired = None
        for n in (0, 1):
            alt, _pre, sel = self.adc_trigger_select(n)
            if not alt or (n, sel) in self._trg_noted:
                continue
            if wired is None:
                wired = subscribe_trigger_sources(self.chip) if self.chip is not None else set()
            if sel not in wired:
                self._trg_noted.add((n, sel))
                self.emit("info", block=self.name, msg=f"SIM_SOPT7: ADC{n} alternate trigger {sel} "
                          f"({ADC_TRGSEL[sel]}) is not modelled; ADC{n} will not be triggered")
        return None
