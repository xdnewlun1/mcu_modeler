"""P89CV51 SPI: SPCR D5H, SPSR AAH, SPDAT 86H (data sheet sec. 6.7 pp.41-43, Tables 27-31, Figs 16-18), bit-level
on the pins P1.4 SS, P1.5 MOSI, P1.6 MISO, P1.7 SPICLK.

* SPCR: SPIE (interrupt enable, with IE.ES), SPEN, DORD (1 = LSB first), MSTR, CPOL (SPICLK idle level), CPHA (0:
  data valid before the leading edge, sampled on it, changed on the trailing edge; 1: changed on the leading edge,
  sampled on the trailing edge), SPR1:SPR0: master SPICLK = fosc / 4, 16, 64, 128 in 12-clock mode, fosc / 2, 8,
  32, 64 in 6-clock mode with CKCON.SPIX2 = 0 (Table 29).
* Master (SPEN = MSTR = 1): writing SPDAT starts 8 clocks; SPICLK and MOSI are driven edge by edge
  (PinBoard.drive_signal "SPI_SCK" / "SPI_MOSI"), MISO is sampled from the pin.  SPIF is set when the 8th bit has
  been sampled; SPDAT then reads the received byte.  Writing SPDAT during a transfer sets WCOL and is ignored.  SS is
  not used by the master (it stays a port pin; drive a slave's select with any port bit).
* Slave (SPEN = 1, MSTR = 0): selected while SS (P1.4) is low; SPICLK edges from the pin shift MOSI in and the
  byte loaded in SPDAT out on MISO (driven only while selected); SPIF after 8 bits.
* Board devices (chipem/devices/spi_flash.py: `{type: spi_flash, spi: SPI, cs: P1.4}` or `--spi-flash
  SPI:cs=P2.0`): `responder(tx, pcs, bits) -> rx` answers a master byte instead of MISO; then no pin edges are
  scheduled (one completion event after 8 SPICLK periods: fast path), the chip-select pin listener frames the
  device's transactions.  `frame_end(cont, eoq)` is called after every byte when the device has no CS pin.
* SPSR: SPIF and WCOL are cleared by software (write 0; writing 1 keeps them).  Interrupt line "SPI" = SPIF & SPIE
  (shared vector 0023H with the UART, enabled by IE.ES).
Not modelled: mode fault (the data sheet lists none), wake-up from idle by a slave transfer (the core wakes on the
SPI interrupt like any other).
"""
from __future__ import annotations

from typing import Any, Callable

from ..registry import register
from ._util import SfrBlock, find_model, listen_pin, periph_div, pin_level

SPIE, SPEN, DORD, MSTR, CPOL, CPHA = 0x80, 0x40, 0x20, 0x10, 0x08, 0x04
SPIF, WCOL = 0x80, 0x40
PINS = {"SS": "P1.4", "MOSI": "P1.5", "MISO": "P1.6", "SCK": "P1.7"}
SIG = {"SS": "SPI_SS", "MOSI": "SPI_MOSI", "MISO": "SPI_MISO", "SCK": "SPI_SCK"}


@register("mcs51.spi")
class Spi(SfrBlock):
    model = "mcs51.spi"
    table = ("SPI",)

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        ov = {"SPCR": {"on_write": self._spcr_w},
              "SPSR": {"on_write": self._spsr_w, "ro_mask": 0x3F},
              "SPDAT": {"on_read": self._spdat_r, "on_write": self._spdat_w}}
        super().__init__(name, base, size, regs, params=params, log=log, overrides=ov)
        self.responder: Callable[[int, int, int], int] | None = None
        self.frame_end: Callable[[bool, bool], None] | None = None
        self.frames: list[tuple[int, int]] = []
        self._rx_reg = 0
        self._busy = False
        self._evs: list = []
        self._tx = 0
        self._rx = 0
        self._bit = 0
        self._sck = "L"
        self._lvl = {k: "H" for k in PINS}
        self._claims: dict[str, str | None] = {}
        self._stopped = False

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        for k in ("SS", "SCK", "MOSI", "MISO"):
            listen_pin(chip, PINS[k], lambda lvl, _k=k: self._pin(_k, lvl))

    def reset(self) -> None:
        self._cancel()
        super().reset()
        self._busy = False
        self._rx_reg = 0
        self._bit = 0
        if self.chip is not None:
            for k in PINS:
                self._lvl[k] = pin_level(self.chip, PINS[k])
            self._roles()
            self._line()

    def _cancel(self) -> None:
        for ev in self._evs:
            self.cancel(ev)
        self._evs = []

    # ------------------------------------------------------------------ config
    def _cr(self) -> int:
        return self.get("SPCR")

    def master(self) -> bool:
        cr = self._cr()
        return bool(cr & SPEN and cr & MSTR)

    def slave(self) -> bool:
        cr = self._cr()
        return bool(cr & SPEN and not cr & MSTR)

    def sck_cycles(self) -> int:
        """Oscillator periods per SPICLK period (master)."""
        div = (4, 16, 64, 128)[self._cr() & 3]
        return div * periph_div(self.chip, "SPIX2") // 12

    def bitrate(self) -> float:
        return self.chip.clock.hz / self.sck_cycles() if self.master() else 0.0

    def _ports(self):
        return find_model(self.chip, "mcs51.port")

    def _claim(self, k: str, out: str | None, on: bool = True) -> None:
        ports = self._ports()
        if ports is None:
            return
        if on:
            ports.claim(PINS[k], SIG[k], out)
            self._claims[k] = out
        elif k in self._claims:
            ports.release(PINS[k], SIG[k])
            self.chip.pins.release_signal(SIG[k])
            del self._claims[k]

    def _roles(self) -> None:
        cpol = "H" if self._cr() & CPOL else "L"
        if self.master():
            self._sck = cpol
            self._claim("SCK", cpol)
            self.chip.pins.drive_signal(SIG["SCK"], cpol)
            self._claim("MOSI", self._claims.get("MOSI") or "L")
            self._claim("MISO", None)
            self._claim("SS", None, False)
        elif self.slave():
            self._claim("SCK", None)
            self._claim("MOSI", None)
            self._claim("SS", None)
            self._slave_select(self._lvl["SS"] == "L")
        else:
            for k in list(self._claims):
                self._claim(k, None, False)

    def _line(self) -> None:
        if self.chip is not None:
            self.chip.arch.set_irq("SPI", bool(self.get("SPSR") & SPIF and self._cr() & SPIE))

    # ------------------------------------------------------------------ SFR hooks
    def _spcr_w(self, blk, reg, old, new):
        if (old ^ new) & (SPEN | MSTR | CPOL):
            self._cancel()
            self._busy = False
            self._roles()
        self._line()
        return None

    def _spsr_w(self, blk, reg, old, new):
        # SPIF / WCOL: cleared by writing 0, a 1 keeps the bit
        v = old & new & (SPIF | WCOL)
        self.set("SPSR", v)
        self._line()
        return v

    def _spdat_r(self, blk, reg):
        return self._rx_reg

    def _spdat_w(self, blk, reg, old, new):
        if self._busy:
            self.set("SPSR", self.get("SPSR") | WCOL)
            return None
        self._tx = new & 0xFF
        if self.master() and not getattr(self, "lp_frozen", False):
            self._start_master()
        return None

    # ------------------------------------------------------------------ master
    def _bits_out(self, b: int) -> list[int]:
        return [(b >> i) & 1 for i in (range(8) if self._cr() & DORD else range(7, -1, -1))]

    def _start_master(self) -> None:
        self._busy = True
        per = self.sck_cycles()
        if self.responder is not None:
            rx = self.responder(self._tx, 0, 8) & 0xFF
            self._evs = [self.at(self.now() + 8 * per, self._master_done, rx)]
            return
        self._out = self._bits_out(self._tx)
        self._rx = 0
        self._bit = 0
        half = max(per // 2, 1)
        t = self.now()
        cpha = bool(self._cr() & CPHA)
        if not cpha:
            self._mosi(self._out[0])
        self._evs = [self.at(t + (k + 1) * half, self._edge, k) for k in range(16)]

    def _mosi(self, bit: int) -> None:
        lvl = "H" if bit else "L"
        self._claims["MOSI"] = lvl
        self.chip.pins.drive_signal(SIG["MOSI"], lvl)

    def _sample(self) -> int:
        return 0 if pin_level(self.chip, PINS["MISO"]) == "L" else 1

    def _edge(self, k: int) -> None:
        cr = self._cr()
        cpol, cpha = bool(cr & CPOL), bool(cr & CPHA)
        leading = k % 2 == 0
        self._sck = ("L" if cpol else "H") if leading else ("H" if cpol else "L")
        self.chip.pins.drive_signal(SIG["SCK"], self._sck)
        i = k // 2
        if leading:
            if cpha:
                self._mosi(self._out[i])
            else:
                self._rx = (self._rx << 1) | self._sample()
        else:
            if cpha:
                self._rx = (self._rx << 1) | self._sample()
            elif i + 1 < 8:
                self._mosi(self._out[i + 1])
        if k == 15:
            rx = self._rx
            if cr & DORD:
                rx = int(f"{rx:08b}"[::-1], 2)
            self._master_done(rx)

    def _master_done(self, rx: int) -> None:
        self._evs = []
        self._busy = False
        self._rx_reg = rx
        self.frames.append((self._tx, rx))
        self.set("SPSR", self.get("SPSR") | SPIF)
        self._line()
        if self.responder is not None and self.frame_end is not None:
            self.frame_end(False, False)

    # ------------------------------------------------------------------ slave
    def _slave_select(self, sel: bool) -> None:
        if sel:
            self._claim("MISO", "L")
            self._bit = 0
            self._rx = 0
            self._out = self._bits_out(self._tx)
            if not self._cr() & CPHA:
                self._miso(self._out[0])
        else:
            self._claim("MISO", None)

    def _miso(self, bit: int) -> None:
        lvl = "H" if bit else "L"
        self._claims["MISO"] = lvl
        self.chip.pins.drive_signal(SIG["MISO"], lvl)

    def _pin(self, k: str, lvl: str) -> None:
        old = self._lvl[k]
        self._lvl[k] = lvl
        if not self.slave() or getattr(self, "lp_frozen", False) or old == lvl:
            return
        if k == "SS":
            self._slave_select(lvl == "L")
            return
        if k != "SCK" or self._lvl["SS"] != "L":
            return
        cr = self._cr()
        idle = "H" if cr & CPOL else "L"
        leading = old == idle and lvl != idle
        cpha = bool(cr & CPHA)
        if leading == (not cpha):                                  # sample edge
            mosi = 0 if self._lvl["MOSI"] == "L" else 1
            self._rx = (self._rx << 1) | mosi
            self._bit += 1
            if self._bit == 8:
                rx = int(f"{self._rx:08b}"[::-1], 2) if cr & DORD else self._rx
                self._rx_reg = rx
                self.frames.append((self._tx, rx))
                self._bit = 0
                self._rx = 0
                self.set("SPSR", self.get("SPSR") | SPIF)
                self._line()
        else:                                                      # shift edge
            if cpha and self._bit < 8:
                self._miso(self._out[self._bit])
            elif not cpha and 0 < self._bit < 8:
                self._miso(self._out[self._bit])

