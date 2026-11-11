"""P89CV51 enhanced UART: SCON 98H, SBUF 99H, SADDR A9H, SADEN B9H (data sheet sec. 6.6 pp.36-41, Tables 24-26;
baud rates sec. 6.5.4-6.5.5 p.35; PCON.SMOD1/SMOD0 Table 4).

Modes (SCON.SM0:SM1; SM0 is SCON.7 while PCON.SMOD0 = 0):
  0  shift register: 8 bits LSB first on RXD with the shift clock on TXD, one bit per peripheral clock (fosc/12, or
     fosc/6 in 6-clock mode with CKCON.SIX2 = 0).  Transmit: TI after 8 bit times, the byte is logged.  Receive
     (sec. 6.6.2; Intel MCS-51 "Mode 0"): starts when REN = 1 and RI = 0 (an SCON write that makes the condition
     true); one bit time later the UART shifts 8 bits in from the RXD pin, LSB first, one per bit time, sampled in
     the middle of the bit while TXD outputs the shift clock (low for the first half of each bit, high for the
     second half: the partner shifts on the rising edge); RI is set at the end of the 8th bit.
  1  8-bit UART, 10 bits (start, 8 data, stop); baud = Timer 1 overflow rate x 2^SMOD1 / 32, or Timer 2 overflow
     rate / 16 when T2CON.TCLK (transmit) / RCLK (receive) is set.
  2  9-bit UART, 11 bits; baud = fosc x 2^SMOD1 / 64 in 12-clock mode (fosc / 32 x 2^SMOD1 in 6-clock mode, SIX2 = 0).
  3  9-bit UART, 11 bits, baud as mode 1.
Frames go through chipem/periph/uartwire.py: TX emits `Frame(byte | TB8 << 8, bits=9)` in modes 2/3 (the 9th data
bit is TB8) and 8-bit frames in mode 1; RX decodes against `rx_format()` (8 or 9 data bits at the receive baud).
TX: writing SBUF starts a transmission at once (the divide-by-16 phase is not modelled); TI is set at the beginning
of the stop bit (`uart` event at the start bit; links deliver at the stop bit).  A write during the stop bit (after
TI) starts the next frame when the stop bit ends; a write while data bits are still shifting is queued the same way
(silicon would corrupt the frame; one `warn`).
RX (sec. 6.6.6-6.6.7): a character is accepted only when REN = 1, the mode is 1-3, RXD (P3.0) is owned by UART_RXD
(latch bit 1) and the clock runs (not in power-down); then SBUF/RB8 load and RI is set only if RI = 0 and
(SM2 = 0, or mode 1 with a valid stop bit, or mode 2/3 with RB8 = 1 and, when SADEN != 0, a given or broadcast
address match: given = (rx & SADEN) == (SADDR & SADEN); broadcast = rx has a 1 wherever SADDR | SADEN has one,
sec. 6.6.9).  With RI still set the frame is "irretrievably lost" (`uart` drop, reason overrun).  Mode 1: RB8 = the
stop bit.  Framing error (invalid stop bit): FE (SCON.7 while SMOD0 = 1) is set and stays set until software clears
it; the character is still loaded (SM2 = 0).
Interrupt line "UART" (shared vector 0023H with SPI) = RI | TI.
Pins: UART_TXD (P3.1) is claimed as an output once the UART is used (mode 1-3 or a transmission) and shows the
latch AND TXD level; UART_RXD (P3.0) is claimed as an input while REN = 1.
"""
from __future__ import annotations

from typing import Any

from ..registry import register
from ..uartwire import Frame, RxChar, RxFormat, UartRx
from ._util import SfrBlock, find_model, fmt_hz, periph_div

RI, TI, RB8, TB8, REN, SM2, SM1, SM0 = 1, 2, 4, 8, 0x10, 0x20, 0x40, 0x80


@register("mcs51.uart")
class Uart(UartRx, SfrBlock):
    model = "mcs51.uart"
    table = ("UART",)

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        ov = {"SCON": {"on_read": self._scon_r, "on_write": self._scon_w, "on_peek": self._scon_r},
              "SBUF": {"on_read": self._sbuf_r, "on_write": self._sbuf_w, "on_peek": self._sbuf_r}}
        self._rx_init()
        super().__init__(name, base, size, regs, params=params, log=log, overrides=ov)
        self.output = bytearray()
        self.tx_listeners: list = []
        self.rx_signal = "UART_RXD"
        self.tx_signal = "UART_TXD"
        self.rx_buf = 0
        self.sm0 = 0
        self.fe = 0
        self._tx_ev = None
        self._tx_queue: list[int] = []
        self._ti_ev = None
        self._stopped = False
        self._used = False
        self._warned_busy = False
        self._m0_evs: list = []                         # mode 0 receive: pending shift-clock / sample events
        self._m0_byte = 0

    # ------------------------------------------------------------------ lifecycle
    def attach(self, chip: Any) -> None:
        super().attach(chip)

    def reset(self) -> None:
        self._m0_cancel()
        self.cancel(self._tx_ev)
        self.cancel(self._ti_ev)
        self._tx_ev = self._ti_ev = None
        self._tx_queue.clear()
        super().reset()
        self.rx_buf = 0
        self.sm0 = 0
        self.fe = 0
        self._stopped = False
        self._used = False
        self._wire_reset()
        if self.chip is not None:
            self._line()
            self._claims()

    # ------------------------------------------------------------------ configuration
    def mode(self) -> int:
        return (self.sm0 << 1) | (self.get("SCON") >> 6 & 1)

    def _sysctrl(self):
        return find_model(self.chip, "mcs51.sysctrl")

    def _smod1(self) -> int:
        sc = self._sysctrl()
        return 1 if sc is not None and sc.smod1() else 0

    def _smod0(self) -> bool:
        sc = self._sysctrl()
        return bool(sc is not None and sc.smod0())

    def _bit_cycles(self, rx: bool) -> float:
        """Oscillator periods per bit (0 = no baud clock)."""
        m = self.mode()
        if m == 0:
            return float(periph_div(self.chip, "SIX2"))
        if m == 2:
            return (64 >> self._smod1()) * periph_div(self.chip, "SIX2") / 12.0
        t2 = find_model(self.chip, "mcs51.timer2")
        if t2 is not None and t2.baud_source(rx):
            p = t2.brg_period()
            return 16.0 * p if p else 0.0
        t1 = find_model(self.chip, "mcs51.timer01")
        p = t1.overflow_period(1) if t1 is not None else None
        if not p:
            return 0.0
        return float(p * (32 >> self._smod1()))

    def _baud(self, rx: bool) -> float:
        c = self._bit_cycles(rx)
        return self.chip.clock.hz / c if c > 0 and self.chip is not None else 0.0

    def baud(self) -> float:
        """Transmit baud rate (0 while no baud clock runs)."""
        return self._baud(False)

    def rx_baud(self) -> float:
        return self._baud(True)

    def data_bits(self) -> int:
        return 9 if self.mode() in (2, 3) else 8

    def char_bits(self) -> int:
        return 1 + self.data_bits() + 1 if self.mode() else 8

    def frame(self) -> str:
        return f"{self.data_bits()}N1"

    def char_ns(self) -> float:
        b = self.rx_baud()
        return self.char_bits() * 1e9 / b if b > 0 else 0.0

    def rx_format(self) -> RxFormat:
        return RxFormat(self.rx_baud(), self.data_bits(), "N", 1.0)

    def config_changed(self) -> None:
        """Baud clock changed (PCON.SMOD1, Timer 1/2 setup): nothing cached; kept for the sysctrl hook."""

    def describe(self) -> str:
        b = self.baud()
        return f"mode {self.mode()}, {fmt_hz(b) if b else 'no baud clock'}"

    # ------------------------------------------------------------------ SCON
    def _scon_r(self, blk, reg):
        v = self.get("SCON") & 0x7F
        return v | ((self.fe if self._smod0() else self.sm0) << 7)

    def _scon_w(self, blk, reg, old, new):
        if self._smod0():
            self.fe = new >> 7 & 1                 # SCON.7 is FE: software clears it
            new = (new & 0x7F) | (self.sm0 << 7)
        else:
            self.sm0 = new >> 7 & 1
        self.set("SCON", new)
        if (new & 0xC0) and not self._used:
            self._used = True
        self._line()
        self._claims()
        self._m0_check()
        return new

    def _claims(self) -> None:
        ports = find_model(self.chip, "mcs51.port")
        if ports is None:
            return
        txp, rxp = ports.pin_for(self.tx_signal), ports.pin_for(self.rx_signal)
        if txp is not None:
            if self._used or self.mode():
                ports.claim(txp, self.tx_signal, "H")
            else:
                ports.release(txp, self.tx_signal)
        if rxp is not None:
            if self.get("SCON") & REN:
                ports.claim(rxp, self.rx_signal, None)
            else:
                ports.release(rxp, self.rx_signal)

    def _line(self) -> None:
        if self.chip is not None:
            self.chip.arch.set_irq("UART", bool(self.get("SCON") & (RI | TI)))

    def _set_scon(self, set_bits: int = 0, clr_bits: int = 0) -> None:
        v = self.get("SCON")
        self.set("SCON", (v | set_bits) & ~clr_bits)
        self._line()

    # ------------------------------------------------------------------ SBUF
    def _sbuf_r(self, blk, reg):
        return self.rx_buf

    def _sbuf_w(self, blk, reg, old, new):
        if not self._used:
            self._used = True
            self._claims()
        if self._tx_ev is not None:
            self._tx_queue.append(new & 0xFF)
            if self._ti_ev is not None and not self._warned_busy:     # before TI: still shifting data bits
                self._warned_busy = True
                self.emit("warn", block=self.name, msg=f"{self.name}: SBUF written while a character is still "
                          "being shifted out (queued; silicon would corrupt the frame)")
            return None
        self._start_tx(new & 0xFF)
        return None

    def _start_tx(self, b: int) -> None:
        m = self.mode()
        if m == 0:
            bit = self._bit_cycles(False)
            self.output.append(b)
            self.emit("uart", uart=self.name, dir="tx", byte=b, mode=0)
            for cb in list(self.tx_listeners):
                cb(self, b)
            self._tx_ev = self.at(self.now() + int(8 * bit), self._tx_done)
            return
        nine = m in (2, 3)
        baud = self.baud()
        data = b | ((self.get("SCON") >> 3 & 1) << 8) if nine else b
        fr = Frame(data, 9 if nine else 8, "N", 1.0, baud)
        self._tx_emit(fr)
        bit = self._bit_cycles(False)
        if bit <= 0:
            bit = self.chip.clock.hz / 115200.0       # no baud clock: the frame still leaves (logged), TI anyway
        nbits = 1 + (9 if nine else 8)                 # TI at the beginning of the stop bit
        self._ti_ev = self.at(self.now() + int(round(nbits * bit)), self._ti_set)
        self._tx_ev = self.at(self.now() + int(round((nbits + 1) * bit)), self._tx_done)

    def _ti_set(self) -> None:
        self._ti_ev = None
        self._set_scon(TI)

    def _tx_done(self) -> None:
        self._tx_ev = None
        if self.mode() == 0:
            self._set_scon(TI)
        if self._tx_queue:
            self._start_tx(self._tx_queue.pop(0))

    # ------------------------------------------------------------------ mode 0 receive
    def _m0_cancel(self) -> None:
        for ev in self._m0_evs:
            self.cancel(ev)
        self._m0_evs = []

    def _m0_check(self) -> None:
        """Mode 0: REN = 1 and RI = 0 start a reception (unless one is running); leaving the condition aborts it."""
        scon = self.get("SCON")
        if self.mode() != 0 or not scon & REN or scon & RI or getattr(self, "lp_frozen", False):
            if self._m0_evs and (self.mode() != 0 or not scon & REN):
                self._m0_cancel()
            return
        if self._m0_evs:
            return
        bit = self._bit_cycles(True)
        t0 = self.now() + int(bit)                     # shifting begins one bit time after the condition is met
        self._m0_byte = 0
        evs = []
        for k in range(8):
            start = t0 + int(k * bit)
            evs.append(self.at(start, self._m0_clock, "L"))
            evs.append(self.at(start + int(bit // 2), self._m0_sample, k))
        evs.append(self.at(t0 + int(8 * bit), self._m0_done))
        self._m0_evs = evs
        if not self._used:
            self._used = True
            self._claims()

    def _m0_clock(self, level: str) -> None:
        ports = find_model(self.chip, "mcs51.port")
        txp = ports.pin_for(self.tx_signal) if ports is not None else None
        if txp is not None:
            ports.claim(txp, self.tx_signal, level)

    def _m0_sample(self, k: int) -> None:
        from ._util import pin_level
        ports = find_model(self.chip, "mcs51.port")
        rxp = ports.pin_for(self.rx_signal) if ports is not None else None
        if pin_level(self.chip, rxp or "P3.0") == "H":
            self._m0_byte |= 1 << k
        self._m0_clock("H")                             # rising shift-clock edge in the middle of the bit

    def _m0_done(self) -> None:
        self._m0_evs = []
        b = self._m0_byte
        self.rx_buf = b
        self.emit("uart", uart=self.name, dir="rx", byte=b, mode=0)
        self._set_scon(RI)

    # ------------------------------------------------------------------ RX
    def rx_pending(self) -> int:
        return self.wire_pending()

    def _rx_block_reason(self) -> str | None:
        if getattr(self, "lp_frozen", False):
            return "clock_stopped"                     # power-down (mcs51.sysctrl Freezer)
        scon = self.get("SCON")
        if not scon & REN:
            return "rx_disabled"
        if self.mode() == 0:
            return "mode0"                             # mode 0 shifts RXD in with its own clock (_m0_check)
        return None

    def _rx_accept(self, ch: RxChar) -> None:
        scon = self.get("SCON")
        m = self.mode()
        data = ch.data & 0xFF
        bit8 = (ch.data >> 8) & 1 if m in (2, 3) else (0 if ch.fe else 1)
        if ch.fe and self._smod0():
            self.fe = 1
        if scon & RI:
            self._rx_drop(data, "overrun")             # RI still set: "the received frame is irretrievably lost"
            return
        if scon & SM2:
            if m == 1 and not bit8:
                self._rx_drop(data, "framing_error")
                return
            if m in (2, 3):
                if not bit8:
                    self._rx_drop(data, "sm2_data")    # multiprocessor mode: data byte ignored
                    return
                if not self._address_match(data):
                    self._rx_drop(data, "address_mismatch")
                    return
        self._rx_event(ch, data | (bit8 << 8) if m in (2, 3) else None)
        self.rx_buf = data
        self.set("SCON", (scon & ~RB8) | (RB8 if bit8 else 0) | RI)
        self._line()

    def _address_match(self, b: int) -> bool:
        saddr, saden = self.get("SADDR"), self.get("SADEN")
        if saden == 0 and saddr == 0:
            return True                                 # all don't-care: feature effectively off (sec. 6.6.9)
        given = (b & saden) == (saddr & saden)
        bmask = (saddr | saden) & 0xFF
        broadcast = (b & bmask) == bmask
        return given or broadcast

