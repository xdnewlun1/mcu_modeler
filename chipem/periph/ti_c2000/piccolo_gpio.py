"""Piccolo GPIO (TMS320F2802x / F2806x, 0x6F80): `ti_c2000.piccolo_gpio`.

Ports A (GPIO0-31) and B (2802x GPIO32-38, 2806x GPIO32-58) work as in `ti_c2000.gpio` (gpio.py: GPxMUX1/2, DIR, PUD,
DAT/SET/CLEAR/TOGGLE, input qualification, GPIOLPMSEL).  Piccolo differences (SPRUI09A 1.4, SPRUH18I 1.4; register
addresses SPRS523P Table 9-29):
* Analog I/O (1.4.1.3, Figure 1-55): AIO2/4/6/10/12/14 share the ADCINA2/A4/A6/B2/B4/B6 pins (the pin table's `AIO`
  key).  AIOMUX1 has two bits per AIOn: 00/01 = AIO enabled (digital I/O), 1x = disabled (reset).  AIODIR 1 = output
  (driving the AIODAT latch), 0 = input.  AIODAT reads the pin level whatever the mode (Table 1-87); writes, AIOSET /
  AIOCLEAR / AIOTOGGLE change the latch.  AIO pins have no pull-up.  The ADC and comparator paths stay connected
  (note A): `aio_drive(name)` tells the analog models when such a pin is driven, so they convert 0 V / VDDA.
  AIOMUX1 / AIODIR are EALLOW protected, the data registers are not (Tables 1-51, 1-53).
* XINT1/XINT2/XINT3 (GPIOXINT1-3SEL, 5 bits): all three select GPIO0-31 (Figure 1-72); there is no XNMI.
* Reset pull-ups: GPAPUD = 0x00000FFF (GPIO0-11, the ePWM pins, off), GPBPUD from the register table (2806x GPIO40-43
  ePWM pins off: 0x00000F00).
* GPIO35-38 are the JTAG pins TDI/TMS/TDO/TCK when TRST = 1; CHIP_EM has no JTAG probe (TRST = 0), so they are
  GPIOs, as in a stand-alone boot.  GPIO19/GPIO38 XCLKIN (XCLK.XCLKINSEL) and USB0DP/DM on GPIO26/27 (GPACTRL2.USBIOEN,
  2806x) are not GPxMUX selections and are not modelled on the pins.
"""
from __future__ import annotations

import re
from typing import Any

from ..registry import register
from .gpio import GPIO

_AIO = re.compile(r"^AIO(\d+)$")


@register("ti_c2000.piccolo_gpio")
class PiccoloGPIO(GPIO):
    implements = ("ti_c2000.gpio",)
    eallow_regs = GPIO.eallow_regs + ["AIOMUX1", "AIODIR", "GPIOXINT3SEL", "GPACTRL2"]

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        super().__init__(name, base, size, regs, params=params, log=log)
        hooks = {"AIOMUX1": (self._aio_cfg, None), "AIODIR": (self._aio_cfg, None),
                 "AIODAT": (self._aio_dat_write, self._aio_dat_read),
                 "AIOSET": (self._aio_set, lambda b, r: 0), "AIOCLEAR": (self._aio_clear, lambda b, r: 0),
                 "AIOTOGGLE": (self._aio_toggle, lambda b, r: 0), "GPIOXINT3SEL": (self._sel_write, None)}
        for rname, (wr, rd) in hooks.items():
            r = self.regs.get(rname)
            if r is not None:
                r.on_write = wr
                if rd is not None:
                    r.on_read = r.on_peek = rd          # computed values only: the debugger may peek them
        self.aio_pins: dict[int, Any] = {}       # AIO number -> pin
        self._aio_by_name: dict[str, int] = {}   # name part (ADCINA2, COMP1A, AIO2) -> AIO number
        self.aio_latch = 0

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        board = getattr(chip, "pins", None)
        self.aio_pins = {}
        self._aio_by_name = {}
        if board is None:
            return
        for p in board.pins:
            fn = (p.functions or {}).get("AIO")
            m = _AIO.match(str(fn or "").strip().upper())
            if m:
                n = int(m.group(1))
                self.aio_pins[n] = p
                for part in p.name.split("/"):
                    self._aio_by_name[part.strip().upper()] = n
                board.input_listeners[p.id].append(self._aio_input)

    def reset(self) -> None:
        super().reset()
        self.aio_latch = 0
        for n in self.aio_pins:
            self._aio_sync(n, quiet=True)

    # ------------------------------------------------------------------ XINT sources
    def xint_pin(self, n: int) -> Any:
        """XINT1-3 select GPIO0-31 (SPRUI09 Figure 1-72); no XNMI on the Piccolo."""
        if n == 0:
            return None
        reg = f"GPIOXINT{n}SEL"
        if reg not in self.regs:
            return None
        return self.pin_for(self.get(reg) & 0x1F)

    # ------------------------------------------------------------------ AIO
    def aio_enabled(self, n: int) -> bool:
        return not (self.get("AIOMUX1") >> (2 * n + 1)) & 1

    def aio_output(self, n: int) -> bool:
        return self.aio_enabled(n) and bool(self.get("AIODIR") >> n & 1)

    def aio_drive(self, name: str) -> str | None:
        """'H' / 'L' when the AIO pin carrying analog signal `name` (ADCINA2, COMP1A, ...) is driven as a digital
        output, else None."""
        n = self._aio_by_name.get(str(name).strip().upper())
        if n is None or not self.aio_output(n):
            return None
        return "H" if self.aio_latch >> n & 1 else "L"

    def _aio_sync(self, n: int, quiet: bool = False) -> None:
        pin = self.aio_pins.get(n)
        board = getattr(self.chip, "pins", None) if self.chip is not None else None
        if pin is None or board is None:
            return
        if not self.aio_enabled(n):
            kw = dict(owner="analog", direction="analog", drive=None)
        elif self.get("AIODIR") >> n & 1:
            kw = dict(owner=f"AIO{n}", direction="out", drive="H" if self.aio_latch >> n & 1 else "L")
        else:
            kw = dict(owner=f"AIO{n}", direction="in", drive=None)
        old = (pin.owner, pin.direction, pin.drive)
        board.mcu_update(pin, pull=None, **kw)
        if not quiet and old != (pin.owner, pin.direction, pin.drive):
            self.emit("info", block=self.name, pin=pin.id, msg=f"{pin.id} -> {kw['owner']} {kw['direction']}")
        self._analog_changed(n)

    def _analog_changed(self, n: int) -> None:
        """A driven AIO changes the voltage the ADC / comparators see: let the comparators re-evaluate."""
        for b in (getattr(self.chip, "peripherals", None) or {}).values():
            if getattr(b, "model", "") == "ti_c2000.comp":
                b._eval()

    def _aio_input(self, pin: Any = None) -> None:
        pass                                      # AIODAT reads the pin live

    def _aio_cfg(self, blk, reg, old, new):
        self.set(reg.name, new)
        ch = old ^ new
        rep = self._rep
        pc = rep.pc() if rep is not None else None
        for n, pin in self.aio_pins.items():
            bit = (3 << 2 * n) if reg.name == "AIOMUX1" else (1 << n)
            if ch & bit:
                self._aio_sync(n)
                if rep is not None:
                    what = ("aio " + ("on" if self.aio_enabled(n) else "off")) if reg.name == "AIOMUX1" else \
                        ("dir out" if self.get("AIODIR") >> n & 1 else "dir in")
                    rep.config(pin, what, pc)
        return None

    def _aio_set_latch(self, v: int) -> None:
        v &= 0x7FFF
        ch = self.aio_latch ^ v
        self.aio_latch = v
        r = self.regs.get("AIODAT")
        if r is not None:
            self._store(r, v)
        for n in self.aio_pins:
            if ch >> n & 1 and self.aio_output(n):
                self._aio_sync(n, quiet=True)

    def _aio_dat_write(self, blk, reg, old, new):
        self._aio_set_latch(new)
        return None

    def _aio_set(self, blk, reg, old, new):
        self._aio_set_latch(self.aio_latch | new)
        return 0

    def _aio_clear(self, blk, reg, old, new):
        self._aio_set_latch(self.aio_latch & ~new)
        return 0

    def _aio_toggle(self, blk, reg, old, new):
        self._aio_set_latch(self.aio_latch ^ new)
        return 0

    def _aio_dat_read(self, blk, reg):
        v = 0
        for n, pin in self.aio_pins.items():
            if self.aio_output(n):
                v |= (self.aio_latch >> n & 1) << n
            elif pin.level() == "H":
                v |= 1 << n
        return v
