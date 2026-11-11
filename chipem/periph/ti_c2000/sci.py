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
SCIxENCLK; data sheet docs/datasheets/ti/tms320f28232.pdf sec. "Serial Communications Interface"): no input queue, like silicon. A
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

Frames (chipem/periph/uartwire.py; SPRU051 §1.2.4-1.2.7, §2.6): characters go out as Frames: 1-8 data bits, in
address-bit mode (SCICCR.ADDR/IDLE MODE = 1) the address bit (WUT: SCICTL1.TXWAKE latched when TXSHF is loaded,
TXWAKE then clears), parity over data + address bit, 1/2 stop bits. In idle-line mode TXWAKE replaces the next
character by an 11-bit idle period (block-start signal; a `uart` event {dir: 'tx', idle: 11}). The receiver decodes
each arriving frame at its own settings (8 SCICLK per bit, a start bit needs 4 low periods, 2-of-3 majority of
periods 4/5/6, only the first stop bit checked, no noise flag): PE (parity, address bit included), FE (stop bit 0),
OE, BRKDT (the line stays low >= 10 bits after a missing stop bit: RX interrupt with RX/BK INT ENA, also in SLEEP;
afterwards characters are dropped (reason break_detected) until SW RESET, as the guide says), RXERROR = OR of them.
These clear only by SW RESET (reading SCIRXBUF clears RXRDY and RXWAKE only). FIFO mode: each character keeps its
FE/PE in SCIRXBUF bits 15/14 (SCIFFFE/SCIFFPE). SLEEP: only address frames (address bit = 1, or the first character
after >= 10 idle bits in idle-line mode) set RXRDY / flags (RXWAKE = 1); others are dropped (reason sleep).
The F2823x SCI cannot transmit a break (no send-break control; a break needs the pin as a GPIO).
Autobaud (SCIFFCT.CDC / ABD / ABDCLR, used by the boot ROM SCI loader): see `_rx_frame`; FFTXDLY is stored only.
"""
from __future__ import annotations

from collections import deque
from typing import Any

from ..registry import register
from ..uartwire import SAMPLES_8, Frame, RxChar, RxFormat, UartRx
from ._util import C2000Block, build_regs, fmt_hz, irq_line, lspclk_hz

# SCICTL1
RXENA, TXENA, SLEEP, TXWAKE, SWRESET, RXERRINTENA = 1 << 0, 1 << 1, 1 << 2, 1 << 3, 1 << 5, 1 << 6
# SCICTL2
TXINTENA, RXBKINTENA, TXEMPTY, TXRDY = 1 << 0, 1 << 1, 1 << 6, 1 << 7
# SCIRXST
RXWAKE, PE, OE, FE, BRKDT, RXRDY, RXERROR = 1 << 1, 1 << 2, 1 << 3, 1 << 4, 1 << 5, 1 << 6, 1 << 7
RX_ERRS = BRKDT | FE | OE | PE
# SCICCR
ADDRIDLE, LOOPBKENA, PARITYENA, PARITY_EVEN, STOPBITS2 = 1 << 3, 1 << 4, 1 << 5, 1 << 6, 1 << 7
# SCIRXBUF (FIFO mode): flags of the character at the top of the FIFO
SCIFFFE, SCIFFPE = 1 << 15, 1 << 14
START_C2000 = (0.5 / 8, 1.5 / 8, 2.5 / 8, 3.5 / 8)    # "four consecutive internal SCICLK periods of zero bits"
IDLE_BLOCK_BITS = 10                                  # idle-line mode: >= 10 idle bits start a block (§1.2.5)
TXWAKE_IDLE_BITS = 11                                 # idle-line mode TXWAKE: an idle period of 11 bits (§1.2.5.3)
# SCIFFTX
TXFFIENA, TXFFINTCLR, TXFFINT, TXFIFOXRESET, SCIFFENA, SCIRST = 1 << 5, 1 << 6, 1 << 7, 1 << 13, 1 << 14, 1 << 15
# SCIFFRX
RXFFIENA, RXFFINTCLR, RXFFINT, RXFIFORESET, RXFFOVRCLR, RXFFOVF = 1 << 5, 1 << 6, 1 << 7, 1 << 13, 1 << 14, 1 << 15
FIFO_DEPTH = 16
# SCIFFCT (autobaud)
ABD, ABDCLR, CDC = 1 << 15, 1 << 14, 1 << 13


@register("ti_c2000.sci")
class SCI(UartRx, C2000Block):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.letter = str(params.get("instance") or name[-1]).upper()
        ov = {
            "SCICTL1": {"on_write": self._ctl1_write},
            "SCICTL2": {"on_write": self._ctl2_write, "ro_mask": 0xFFFC},
            "SCIRXBUF": {"on_read": self._rxbuf_read, "on_peek": self._rxbuf_peek},
            "SCIRXEMU": {"on_read": self._rxemu_read, "on_peek": self._rxemu_read},
            "SCITXBUF": {"on_write": self._txbuf_write},
            "SCIFFTX": {"on_write": self._fftx_write},
            "SCIFFRX": {"on_write": self._ffrx_write},
            "SCICCR": {"on_write": self._ccr_write},
            "SCIHBAUD": {"on_write": self._baud_write},
            "SCILBAUD": {"on_write": self._baud_write},
            "SCIFFCT": {"on_write": self._ffct_write},
        }
        regs = build_regs((name, f"SCI{self.letter}", "SCIA"), size, ov, params.get("regs_file"))
        self._rx_init()
        self._loop_evs: list = []
        super().__init__(name, base, size, regs, params=params, log=log)
        self.fifo_depth = int(self.params.get("fifo_depth", FIFO_DEPTH))   # Piccolo: 4 (SPRUI09 8.1/9.1/10.1)
        self.gate = f"SCI{self.letter}ENCLK"
        self.output = bytearray()
        self.tx_listeners: list = []
        self.rx_signal = f"SCIRXD{self.letter}"
        self.tx_signal = f"SCITXD{self.letter}"          # bit-level waveform on the TX pin (uartwire._tx_emit)
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
        return (1 + self.data_bits() + (1 if ccr & ADDRIDLE else 0) + (1 if ccr & PARITYENA else 0)
                + (2 if ccr & STOPBITS2 else 1))

    def _parity(self) -> str:
        ccr = self.get("SCICCR")
        return ("E" if ccr & PARITY_EVEN else "O") if ccr & PARITYENA else "N"

    def frame(self) -> str:
        ccr = self.get("SCICCR")
        return f"{self.data_bits()}{self._parity()}{2 if ccr & STOPBITS2 else 1}" + ("+A" if ccr & ADDRIDLE else "")

    def tx_frame_for(self, b: int, wut: int = 0) -> Frame:
        """Frame (SPRU051 §1.2.7, Figure 1-7): start, 1-8 data bits LSB first, the address bit (address-bit mode:
        WUT, i.e. TXWAKE latched with the character), parity over data + address bit, 1 or 2 stop bits."""
        ccr = self.get("SCICCR")
        return Frame(b, self.data_bits(), self._parity(), 2.0 if ccr & STOPBITS2 else 1.0, self.baud(),
                     addr=wut if ccr & ADDRIDLE else None)

    def rx_format(self) -> RxFormat:
        """Receiver (SPRU051 §1.2.7): 8 SCICLK periods per bit, a start bit needs four consecutive 0 periods,
        every other bit is the 2-of-3 majority of the 4th/5th/6th periods; only the first stop bit is checked; no
        noise flag."""
        ccr = self.get("SCICCR")
        return RxFormat(self.baud(), self.data_bits(), self._parity(), 2.0 if ccr & STOPBITS2 else 1.0,
                        addr=bool(ccr & ADDRIDLE), samples=SAMPLES_8, start=START_C2000, start_all=True)

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
            if len(self.tx_fifo) >= self.fifo_depth:
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
        """SCITXBUF -> TXSHF; TXWAKE -> WUT and TXWAKE is cleared (§1.2.5.3). Idle-line mode with WUT = 1: the
        character is suppressed and an idle period of 11 bits goes out instead (block-start signal)."""
        self._ctl2(clr_bits=TXEMPTY)
        c1 = self.get("SCICTL1")
        wut = 1 if c1 & TXWAKE else 0
        if wut:
            self.set("SCICTL1", c1 & ~TXWAKE)
        ccr = self.get("SCICCR")
        if wut and not ccr & ADDRIDLE:
            bd = self.baud()
            self.emit("uart", uart=self.name, dir="tx", idle=TXWAKE_IDLE_BITS)
            t = TXWAKE_IDLE_BITS * 1e9 / bd if bd > 0 else 1.0
            self._shift_ev = self.chip.sched.in_ns(t, self._shift_done)
            return
        fr = self.tx_frame_for(b, wut)
        self._tx_emit(fr)
        t = self.char_ns()
        if ccr & LOOPBKENA:
            self._loop_evs.append(self.chip.sched.in_ns(t if t > 0 else 1.0, self._loop_arrive, fr))
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

    def _loop_arrive(self, fr: Frame) -> None:
        if self._loop_evs:
            self._loop_evs.pop(0)               # loopback characters arrive in order
        self._rx_frame(fr, "loop")

    # ------------------------------------------------------------------ autobaud (SPRU051 2.7, SCIFFCT)
    def _ffct_write(self, blk, reg, old, new):
        """ABD (bit 15) is read-only, ABDCLR (bit 14) writes 1 to clear ABD and reads 0; CDC (13), FFTXDLY stored."""
        val = (new & 0x20FF) | (old & ABD)
        if new & ABDCLR:
            val &= ~ABD
        return val

    def _rx_frame(self, fr: Frame, src: str, t_end: float | None = None) -> None:
        """With SCIFFCT.CDC = 1 the autobaud logic watches the line: an 'A' or 'a' (the only characters it can lock
        on) sets SCIHBAUD/SCILBAUD to the sender's bit rate (BRR = LSPCLK / (8 x baud) - 1) and SCIFFCT.ABD, and is
        then received normally (TI's boot ROM reads and echoes it).  Frames carry their bit rate, so the lock is
        exact to the BRR rounding.  Any other character while CDC = 1 is discarded (`uart` drop, reason
        autobaud): on silicon it would lock on a wrong rate."""
        if (self.get("SCIFFCT") & CDC and src != "loop" and not fr.brk
                and self._rx_block_reason() is None):
            if fr.data & 0xFF not in (0x41, 0x61):
                self._rx_drop(fr.data, "autobaud")
                return
            baud = fr.baud or self.baud()
            hz = self.module_hz()
            if baud > 0 and hz > 0:
                brr = max(1, min(0xFFFF, int(round(hz / (8.0 * baud))) - 1))
                self.set("SCIHBAUD", brr >> 8)
                self.set("SCILBAUD", brr & 0xFF)
                if self._ln is not None:
                    self._ln.reset()
                self.emit("info", block=self.name, msg=f"{self.name}: autobaud lock on 0x{fr.data & 0xFF:02X} at "
                          f"{baud:.0f} baud -> BRR {brr} ({self.baud():.0f} baud)")
            self.set("SCIFFCT", self.get("SCIFFCT") | ABD)
        super()._rx_frame(fr, src, t_end)

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

    def _rx_accept(self, ch: RxChar) -> None:
        """A decoded character (SPRU051 §1.2.4-1.2.8, §2.6): break detect (BRKDT: the line stays low >= 10 bits after
        a missing first stop bit; RX interrupt even in SLEEP; further characters are not received until SW RESET),
        sleep / wake-up (address-bit mode: address bit = 1; idle-line mode: >= 10 idle bits before the character;
        RXWAKE), FE (first stop bit 0), PE (address bit included), OE; FIFO mode keeps FE/PE per character
        (SCIRXBUF.SCIFFFE/SCIFFPE). Error flags clear only by SW RESET."""
        st = self.get("SCIRXST")
        b = ch.data & ((1 << self.data_bits()) - 1)
        if st & BRKDT:
            self._rx_drop(b, "break_detected")      # "the SCI must be reset by toggling the SW RESET bit"
            return
        ccr = self.get("SCICCR")
        n = self.data_bits() + (1 if ccr & ADDRIDLE else 0) + (1 if ccr & PARITYENA else 0)
        brkdt = bool(ch.brk and ch.brk >= n + 2 + 10 - 0.01)
        if ccr & ADDRIDLE:
            wake = bool(ch.addr)
        else:
            wake = ch.idle >= IDLE_BLOCK_BITS - 0.01
        if self.get("SCICTL1") & SLEEP and not wake:
            if brkdt:
                self.emit("uart", uart=self.name, dir="rx", brk=round(ch.brk, 1))
                self.set("SCIRXST", st | BRKDT | RXERROR)
                self._update_lines()
            else:
                self._rx_drop(b, "sleep")           # no RXRDY, no error flags in sleep mode (§1.2.4)
            return
        err = (FE if ch.fe else 0) | (PE if ch.pe else 0) | (BRKDT if brkdt else 0)
        if self.fifo_mode():
            if len(self.rx_fifo) >= self.fifo_depth:
                self._rx_drop(b, "overrun")
                self.set("SCIFFRX", self.get("SCIFFRX") | RXFFOVF)
                return
            self._rx_event(ch, b)
            self.rx_fifo.append(b | (SCIFFFE if ch.fe else 0) | (SCIFFPE if ch.pe else 0))
            st = (st & ~RXWAKE) | (RXWAKE if wake else 0) | err
            self.set("SCIRXST", st | (RXERROR if st & RX_ERRS else 0))
            self._sync_ff()
            return
        if st & RXRDY:                          # previous character overwritten and lost
            self._rx_drop(self._rx_byte, "overrun")
            st |= OE
        self._rx_event(ch, b)
        self._rx_byte = b
        self.set("SCIRXBUF", b)
        self.set("SCIRXEMU", b)
        st = (st & ~RXWAKE) | (RXWAKE if wake else 0) | err | RXRDY
        self.set("SCIRXST", st | (RXERROR if st & RX_ERRS else 0))
        self._update_lines()

    def _rxbuf_read(self, blk, reg):
        if self.fifo_mode():
            if self.rx_fifo:
                w = self.rx_fifo.popleft()
                self._rx_byte = w & 0xFF
                self._sync_ff()
                return w
            return self._rx_byte
        st = self.get("SCIRXST")
        if st & (RXRDY | RXWAKE):
            self.set("SCIRXST", st & ~(RXRDY | RXWAKE))   # BRKDT and the error flags stay until SW RESET
            self._update_lines()
        return self._rx_byte

    def _rxbuf_peek(self, blk, reg):
        if self.fifo_mode() and self.rx_fifo:
            return self.rx_fifo[0]                      # the FIFO head, not popped
        return self._rx_byte

    def _rxemu_read(self, blk, reg):
        if self.fifo_mode() and self.rx_fifo:
            return self.rx_fifo[0] & 0xFF
        return self._rx_byte
