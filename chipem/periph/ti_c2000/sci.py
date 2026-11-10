"""C28x SCI (UART): SCI-A 0x7050, SCI-B 0x7750 (SCI-C 0x7770 on F2833x/F28235) — SPRUFZ5.

Registers (16-bit): SCICCR (char length, parity, stop bits, loopback), SCICTL1 (RXENA, TXENA, SWRESET), SCIHBAUD/
SCILBAUD (BRR; baud = LSPCLK / ((BRR + 1) × 8), BRR = 0 → LSPCLK / 16), SCICTL2 (TXINTENA, RXBKINTENA, TXEMPTY,
TXRDY), SCIRXST (RXRDY, OE, BRKDT, RXERROR), SCIRXEMU, SCIRXBUF, SCITXBUF, SCIFFTX/SCIFFRX/SCIFFCT (16-deep FIFOs),
SCIPRI.

TX: a write to SCITXBUF (SWRESET = 1, TXENA = 1) goes to the shift register when it is idle (TXRDY stays 1, TXEMPTY 0
for one character time) or waits in TXBUF (TXRDY = 0).  The byte is "on the wire" when it enters the shift register:
it is appended to `output`, logged as a `uart` event and passed to `tx_listeners` (board links), and looped back
with SCICCR.LOOPBKENA.  With SCIFFTX.SCIFFENA the 16-deep TX FIFO feeds the shift register; TXFFST counts it and
TXFFINT is set while TXFFST <= TXFFIL (cleared by TXFFINTCLR, re-set while the condition holds).
RX (SPRUFZ5 SCI reference guide: SCICTL1.RXENA/SW RESET, SCIRXST.RXRDY/OE/RXERROR, SCIFFRX.RXFFOVF; PCLKCR0
SCIxENCLK; data sheet tms320f28232.pdf sec. "Serial Communications Interface"): no input queue, like silicon. A
character arrives at its stop bit (host input through `feed()` is paced at this SCI's character time, board links
call `receive()` at the stop-bit time, SCICCR.LOOPBKENA loops TX back one character time later; see
chipem/periph/uartwire.py) and is dropped (`uart` event dir 'drop', reason) when PCLKCR0 SCIxENCLK = 0 (clock_gated),
SCICTL1.SW RESET = 0 or SCIFFTX.SCIRST = 0 or, in FIFO mode, SCIFFRX.RXFIFORESET = 0 (reset), SCICTL1.RXENA = 0
(rx_disabled) or no pin is muxed to SCIRXDx (pin_not_muxed; not checked for loopback). Otherwise:
* without FIFO (1-character SCIRXBUF): RXRDY = 1 and SCIRXBUF returns it. If RXRDY is still set the previous
  character is overwritten and lost ("OE ... the previous character is overwritten and lost"): OE and RXERROR are
  set, the lost one is logged as a 'drop' with reason overrun. OE/RXERROR clear only on SW RESET (or system reset).
* with SCIFFENA (16-deep RX FIFO): the character is pushed (RXFFST counts it). A 17th character sets RXFFOVF (cleared
  by RXFFOVRCLR) and is not stored (we keep the FIFO contents; the guide's "the first received word is lost" is
  read as "the word that overflowed").
Reading SCIRXBUF clears RXRDY / pops the FIFO.  RXFFINT is set while RXFFST >= RXFFIL.
Interrupt lines (level, latched on rising edges by the PIE): RX = RXBKINTENA & RXRDY (+ RXERRINTENA & RXERROR), or
RXFFIENA & RXFFINT in FIFO mode → SCIRXINTx; TX = TXINTENA & TXRDY, or TXFFIENA & TXFFINT → SCITXINTx.
SWRESET = 0 holds the SCI in reset: TXRDY = TXEMPTY = 1, RX flags clear, TXBUF writes are ignored.
"""
from __future__ import annotations

from collections import deque
from typing import Any

from ..registry import register
from ..uartwire import UartRx
from ._util import C2000Block, build_regs, fmt_hz, irq_line, lspclk_hz

# SCICTL1
RXENA, TXENA, SWRESET, RXERRINTENA = 1 << 0, 1 << 1, 1 << 5, 1 << 6
# SCICTL2
TXINTENA, RXBKINTENA, TXEMPTY, TXRDY = 1 << 0, 1 << 1, 1 << 6, 1 << 7
# SCIRXST
OE, BRKDT, RXRDY, RXERROR = 1 << 3, 1 << 5, 1 << 6, 1 << 7
# SCICCR
LOOPBKENA, PARITYENA, PARITY_EVEN, STOPBITS2 = 1 << 4, 1 << 5, 1 << 6, 1 << 7
# SCIFFTX
TXFFIENA, TXFFINTCLR, TXFFINT, TXFIFOXRESET, SCIFFENA, SCIRST = 1 << 5, 1 << 6, 1 << 7, 1 << 13, 1 << 14, 1 << 15
# SCIFFRX
RXFFIENA, RXFFINTCLR, RXFFINT, RXFIFORESET, RXFFOVRCLR, RXFFOVF = 1 << 5, 1 << 6, 1 << 7, 1 << 13, 1 << 14, 1 << 15
FIFO_DEPTH = 16


@register("ti_c2000.sci")
class SCI(UartRx, C2000Block):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.letter = str(params.get("instance") or name[-1]).upper()
        ov = {
            "SCICTL1": {"on_write": self._ctl1_write},
            "SCICTL2": {"on_write": self._ctl2_write, "ro_mask": 0xFFFC},
            "SCIRXBUF": {"on_read": self._rxbuf_read},
            "SCIRXEMU": {"on_read": self._rxemu_read},
            "SCITXBUF": {"on_write": self._txbuf_write},
            "SCIFFTX": {"on_write": self._fftx_write},
            "SCIFFRX": {"on_write": self._ffrx_write},
            "SCICCR": {"on_write": self._ccr_write},
            "SCIHBAUD": {"on_write": self._baud_write},
            "SCILBAUD": {"on_write": self._baud_write},
        }
        regs = build_regs((name, f"SCI{self.letter}", "SCIA"), size, ov, params.get("regs_file"))
        self._rx_init()
        self._loop_evs: list = []
        super().__init__(name, base, size, regs, params=params, log=log)
        self.gate = f"SCI{self.letter}ENCLK"
        self.output = bytearray()
        self.tx_listeners: list = []
        self.rx_signal = f"SCIRXD{self.letter}"
        self.rx_fifo: deque[int] = deque()
        self.tx_fifo: deque[int] = deque()
        self._txbuf: int | None = None          # non-FIFO TXBUF holding a byte
        self._shift_ev = None
        self._rx_byte = 0
        self._rx_line = self._tx_line = False
        self._warned_tx = False
        self.rx_irq = params.get("rx_irq")
        self.tx_irq = params.get("tx_irq")

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        if self.rx_irq is None:
            self.rx_irq = irq_line(chip, f"SCIRXINT{self.letter}", {"A": "9.1", "B": "9.3", "C": "8.5"}[self.letter])
        if self.tx_irq is None:
            self.tx_irq = irq_line(chip, f"SCITXINT{self.letter}", {"A": "9.2", "B": "9.4", "C": "8.6"}[self.letter])

    def reset(self) -> None:
        self._cancel_all()
        self._set_lines(False, False)
        super().reset()
        self.rx_fifo.clear()
        self.tx_fifo.clear()
        self._txbuf = None
        self._rx_byte = 0
        self._sync_ff()
        self._wire_reset()
        # the output log survives a reset (the user's view of the wire); the host keeps sending what it has left

    def _cancel_all(self) -> None:
        if self.chip is None:
            return
        for ev in (self._shift_ev, *self._loop_evs):
            if ev is not None:
                self.chip.sched.cancel(ev)
        self._shift_ev = None
        self._loop_evs = []

    # ------------------------------------------------------------------ timing / framing
    def brr(self) -> int:
        return ((self.get("SCIHBAUD") & 0xFF) << 8) | (self.get("SCILBAUD") & 0xFF)

    def module_hz(self) -> float:
        return lspclk_hz(self.chip) if self.chip is not None else 0.0

    def baud(self) -> float:
        """Baud rate; 0 while the SCI is held in software reset (no baud generator yet, as board links expect)."""
        if self._in_reset():
            return 0.0
        brr = self.brr()
        hz = self.module_hz()
        return hz / 16.0 if brr == 0 else hz / ((brr + 1) * 8.0)

    def data_bits(self) -> int:
        return (self.get("SCICCR") & 7) + 1

    def char_bits(self) -> int:
        ccr = self.get("SCICCR")
        return 1 + self.data_bits() + (1 if ccr & PARITYENA else 0) + (2 if ccr & STOPBITS2 else 1)

    def frame(self) -> str:
        ccr = self.get("SCICCR")
        par = ("E" if ccr & PARITY_EVEN else "O") if ccr & PARITYENA else "N"
        return f"{self.data_bits()}{par}{2 if ccr & STOPBITS2 else 1}"

    def char_ns(self) -> float:
        b = self.baud()
        return self.char_bits() * 1e9 / b if b > 0 else 0.0

    # ------------------------------------------------------------------ state helpers
    def _in_reset(self) -> bool:
        return not self.get("SCICTL1") & SWRESET

    def fifo_mode(self) -> bool:
        return bool(self.get("SCIFFTX") & SCIFFENA)

    def _sync_ff(self) -> None:
        """Recompute TXFFST/TXFFINT/RXFFST/RXFFINT and the interrupt lines."""
        tx = self.get("SCIFFTX")
        txst = len(self.tx_fifo)
        tx = (tx & ~(0x1F << 8)) | (txst << 8)
        if self.fifo_mode() and txst <= (tx & 0x1F):
            tx |= TXFFINT
        self.set("SCIFFTX", tx)
        rx = self.get("SCIFFRX")
        rxst = len(self.rx_fifo)
        rx = (rx & ~(0x1F << 8)) | (rxst << 8)
        if self.fifo_mode() and rxst >= (rx & 0x1F) and rxst > 0:
            rx |= RXFFINT
        self.set("SCIFFRX", rx)
        self._update_lines()

    def _update_lines(self) -> None:
        if self.fifo_mode():
            tx = self.get("SCIFFTX")
            rx = self.get("SCIFFRX")
            txl = bool(tx & TXFFIENA and tx & TXFFINT)
            rxl = bool(rx & RXFFIENA and rx & RXFFINT)
        else:
            c2 = self.get("SCICTL2")
            st = self.get("SCIRXST")
            txl = bool(c2 & TXINTENA and c2 & TXRDY)
            rxl = bool(c2 & RXBKINTENA and st & (RXRDY | BRKDT))
        st = self.get("SCIRXST")
        if self.get("SCICTL1") & RXERRINTENA and st & RXERROR:
            rxl = True
        self._set_lines(rxl, txl)

    def _set_lines(self, rxl: bool, txl: bool) -> None:
        if self.chip is None:
            self._rx_line, self._tx_line = rxl, txl
            return
        if rxl != self._rx_line:
            self._rx_line = rxl
            self.irq(self.rx_irq, rxl)
        if txl != self._tx_line:
            self._tx_line = txl
            self.irq(self.tx_irq, txl)

    def _ctl2(self, set_bits: int = 0, clr_bits: int = 0) -> None:
        self.set("SCICTL2", (self.get("SCICTL2") | set_bits) & ~clr_bits)

    # ------------------------------------------------------------------ control writes
    def _ctl1_write(self, blk, reg, old, new):
        self.set("SCICTL1", new)
        if not new & SWRESET:
            # software reset: operating flags to their reset state
            if self._shift_ev is not None:
                self.chip.sched.cancel(self._shift_ev)
                self._shift_ev = None
            self._txbuf = None
            self._ctl2(TXRDY | TXEMPTY)
            self.set("SCIRXST", 0)
        if (new ^ old) & (SWRESET | TXENA | RXENA):
            self.emit("info", block=self.name, msg=f"{self.name}: "
                      + ("in reset" if not new & SWRESET else
                         f"TX {'on' if new & TXENA else 'off'}, RX {'on' if new & RXENA else 'off'}, "
                         f"{self.baud():.0f} baud {self.frame()} (LSPCLK {fmt_hz(self.module_hz())})"))
        self._update_lines()
        return None

    def _ctl2_write(self, blk, reg, old, new):
        self.set("SCICTL2", new)
        self._update_lines()
        return None

    def _ccr_write(self, blk, reg, old, new):
        return None

    def _baud_write(self, blk, reg, old, new):
        return new & 0xFF

    def _fftx_write(self, blk, reg, old, new):
        val = new & ~(TXFFINTCLR | TXFFINT | (0x1F << 8))
        val |= old & TXFFINT
        if new & TXFFINTCLR:
            val &= ~TXFFINT
        if not new & TXFIFOXRESET:
            self.tx_fifo.clear()
        if not new & SCIRST:
            self.tx_fifo.clear()
            self.rx_fifo.clear()
        self.set("SCIFFTX", val)
        self._sync_ff()
        return self.get("SCIFFTX")

    def _ffrx_write(self, blk, reg, old, new):
        val = new & ~(RXFFINTCLR | RXFFINT | RXFFOVRCLR | RXFFOVF | (0x1F << 8))
        val |= old & (RXFFINT | RXFFOVF)
        if new & RXFFINTCLR:
            val &= ~RXFFINT
        if new & RXFFOVRCLR:
            val &= ~RXFFOVF
        if not new & RXFIFORESET:
            self.rx_fifo.clear()
        self.set("SCIFFRX", val)
        self._sync_ff()
        return self.get("SCIFFRX")

    # ------------------------------------------------------------------ TX
    def _txbuf_write(self, blk, reg, old, new):
        self.check_gate()
        b = new & 0xFF & ((1 << self.data_bits()) - 1)
        c1 = self.get("SCICTL1")
        if not c1 & SWRESET or not c1 & TXENA:
            if not self._warned_tx:
                self._warned_tx = True
                why = "SCICTL1.SWRESET=0 (held in reset)" if not c1 & SWRESET else "transmitter disabled (TXENA=0)"
                self.emit("warn", block=self.name, msg=f"{self.name}: SCITXBUF write ignored, {why}")
            return b
        if self.fifo_mode():
            if not self.get("SCIFFTX") & TXFIFOXRESET:
                return b
            if len(self.tx_fifo) >= FIFO_DEPTH:
                self.emit("warn", block=self.name, msg=f"{self.name}: TX FIFO overflow, byte 0x{b:02X} lost")
                return b
            self.tx_fifo.append(b)
            if self._shift_ev is None:
                self._start_shift(self.tx_fifo.popleft())
            self._sync_ff()
            return b
        if self._shift_ev is None:
            self._ctl2(TXRDY, 0)
            self._set_lines(self._rx_line, False)      # TXRDY pulses low while TXBUF moves to the shifter
            self._start_shift(b)
            self._update_lines()
        else:
            self._txbuf = b
            self._ctl2(clr_bits=TXRDY)
            self._update_lines()
        return b

    def _start_shift(self, b: int) -> None:
        self._ctl2(clr_bits=TXEMPTY)
        self.output.append(b)
        self.emit("uart", uart=self.name, dir="tx", byte=b)
        for cb in list(self.tx_listeners):
            cb(self, b)
        t = self.char_ns()
        if self.get("SCICCR") & LOOPBKENA:
            self._loop_evs.append(self.chip.sched.in_ns(t if t > 0 else 1.0, self._loop_arrive, b))
        self._shift_ev = self.chip.sched.in_ns(t if t > 0 else 1.0, self._shift_done)

    def _shift_done(self) -> None:
        self._shift_ev = None
        if self.fifo_mode() and self.tx_fifo:
            self._start_shift(self.tx_fifo.popleft())
            self._sync_ff()
            return
        if self._txbuf is not None:
            b, self._txbuf = self._txbuf, None
            self._ctl2(TXRDY)
            self._start_shift(b)
            self._update_lines()
            return
        self._ctl2(TXEMPTY | TXRDY)
        self._sync_ff()

    # ------------------------------------------------------------------ RX
    def rx_pending(self) -> int:
        return self.wire_pending() + len(self.rx_fifo) + (1 if self.get("SCIRXST") & RXRDY else 0)

    def _loop_arrive(self, b: int) -> None:
        if self._loop_evs:
            self._loop_evs.pop(0)               # loopback characters arrive in order
        self._rx_arrive(b, "loop")

    def _rx_block_reason(self) -> str | None:
        sc = self._sysctrl
        if self.gate is not None and sc is not None and not sc.clock_enabled(self.gate):
            return "clock_gated"
        if self.clk_stopped:
            return "clock_stopped"                  # STANDBY / HALT: LSPCLK is off
        c1 = self.get("SCICTL1")
        if not c1 & SWRESET or not self.get("SCIFFTX") & SCIRST:
            return "reset"
        if self.fifo_mode() and not self.get("SCIFFRX") & RXFIFORESET:
            return "reset"
        if not c1 & RXENA:
            return "rx_disabled"
        return None

    def _rx_accept(self, b: int) -> None:
        b &= (1 << self.data_bits()) - 1
        if self.fifo_mode():
            if len(self.rx_fifo) >= FIFO_DEPTH:
                self._rx_drop(b, "overrun")
                self.set("SCIFFRX", self.get("SCIFFRX") | RXFFOVF)
                return
            self.emit("uart", uart=self.name, dir="rx", byte=b)
            self.rx_fifo.append(b)
            self._sync_ff()
            return
        st = self.get("SCIRXST")
        if st & RXRDY:                          # previous character overwritten and lost
            self._rx_drop(self._rx_byte, "overrun")
            st |= OE | RXERROR
        self.emit("uart", uart=self.name, dir="rx", byte=b)
        self._rx_byte = b
        self.set("SCIRXBUF", b)
        self.set("SCIRXEMU", b)
        self.set("SCIRXST", st | RXRDY)
        self._update_lines()

    def _rxbuf_read(self, blk, reg):
        if self.fifo_mode():
            if self.rx_fifo:
                b = self.rx_fifo.popleft()
                self._rx_byte = b
                self._sync_ff()
                return b
            return self._rx_byte
        st = self.get("SCIRXST")
        if st & RXRDY:
            self.set("SCIRXST", st & ~(RXRDY | BRKDT))
            self._update_lines()
        return self._rx_byte

    def _rxemu_read(self, blk, reg):
        if self.fifo_mode() and self.rx_fifo:
            return self.rx_fifo[0]
        return self._rx_byte
