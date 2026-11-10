"""Kinetis UART (UART0-4), byte-level model with the transmit/receive data buffers (FIFOs).

Sources: K22 Sub-Family Reference Manual (K22P100M120SF5V2RM, Rev. 5): ch. 49 "UART" (§49.3.5 S1, §49.3.8 D,
§49.3.16-49.3.22 PFIFO/CFIFO/SFIFO/TWFIFO/TCFIFO/RWFIFO/RCFIFO, §49.4.1.3 character transmission, §49.4.2.3
character reception, §49.7 DMA, §49.8.1 data buffer operation, §49.8.4 overrun, §49.8.10 item 4 "OR is set only if
the data buffer (FIFO) does not have sufficient room"); §3.9.5.1 items 6/7 (UART0/UART1 have 8-entry TX and RX
FIFOs, UART2-4 1-entry); §3.9.5.3 (status vs error interrupt sources); ch. 12 SIM_SCGC4/SCGC1 clock gates.

Clocking: baud = module clock / (16 * (SBR + BRFA/32)), SBR = BDH[4:0]:BDL, BRFA = C4[4:0]; module clock = core clock
for UART0/UART1, bus clock for UART2-4 (§3.9.5.1 item 2, clockutil.bus_hz).  Character time = (1 start + 8|9 data +
1|2 stop) bits / baud.  SBR = 0 means "baud generator off": characters then shift out instantly.

Buffers: `params.fifo_depth` (1, 4, 8, ... 128; default 1) is the implemented size; PFIFO.TXFIFOSIZE/RXFIFOSIZE
(read-only) report it with the §49.3.16 encoding (1 -> 000, 4 -> 001, 8 -> 010, ...), so PFIFO resets to 0x22 on
UART0/1 (the SVD says 0x00 because the value is per instance).  The effective depth is that size when PFIFO.TXFE /
RXFE is set and 1 otherwise ("legacy", the reset state).  The transmitter is double buffered as on silicon: the
buffer (depth entries) feeds a shift register.

TX (§49.4.1.3, §49.3.5 TDRE/TC, §49.3.20): writing D with C2.TE set appends the byte to the buffer; a full buffer
loses the byte and sets SFIFO.TXOF (§49.3.18).  When the shift register is idle the oldest byte moves into it: that
is when it is appended to `self.output`, logged as a `uart` event {uart, dir: 'tx', byte} and handed to
`tx_listeners` (board links deliver it one character time later, at its stop bit).  S1.TDRE = (TCFIFO <=
TWFIFO.TXWATER), S1.TC = buffer empty and shift register idle.  Setting C2.TE (0 -> 1) first shifts out a one
character idle preamble.  With TE cleared the character in the shift register completes and the buffer is held.
TCFIFO excludes the character being shifted.  CFIFO.TXFLUSH empties the buffer, not the shift register.

RX (§49.4.2.3, §49.3.5 RDRF/OR, §49.8.4; see chipem/periph/uartwire.py for arrival and the "is the receiver
listening" rule): a character arrives at its stop bit and is dropped (`uart` event dir 'drop', reason) when C2.RE = 0
(rx_disabled), the SIM clock gate is off (clock_gated) or no pin offering UARTn_RX is muxed to it (pin_not_muxed).
Otherwise it is stored in the receive buffer (logged dir 'rx').  S1.RDRF = (RCFIFO >= RWFIFO.RXWATER).  If the buffer
is full the character is lost (reason overrun) and S1.OR and SFIFO.RXOF set; while OR is set no character is stored
even if there is room, and RDRF cannot newly assert.  OR clears by reading S1 with OR set and then reading D.  Reading
D pops the oldest byte; reading D with the buffer empty sets SFIFO.RXUF and returns the last byte read (silicon then
misaligns its FIFO pointers until RXFLUSH, §49.3.5 note: not modelled).  CFIFO.RXFLUSH empties the buffer.
SFIFO.RXOF/TXOF/RXUF are w1c; TXEMPT/RXEMPT follow the buffers.  Watermark writes do not clear TDRE/RDRF.

IRQs (§3.9.5.3): "UART<n>_RX_TX" = (TDRE & TIE & !C5.TDMAS) | (TC & TCIE) | (RDRF & RIE & !C5.RDMAS);
"UART<n>_ERR" = (OR & C3.ORIE) | (RXOF & CFIFO.RXOFE) | (TXOF & CFIFO.TXOFE) | (RXUF & CFIFO.RXUFE).
DMA (§49.7): with C5.RDMAS (and C2.RIE) RDRF raises the DMAMUX request "UART<n> RX" instead of the interrupt; with
C5.TDMAS (and C2.TIE) TDRE raises "UART<n> TX" (`dma_request`); the request is re-raised while the flag holds, so the
DMA drains/fills the FIFO down/up to the watermark.  Source numbers default to the K22 DMAMUX table (UART0 RX/TX =
2/3, UART1 4/5, UART2 6/7, UART3 8/9, UART4 10 (RX and TX share it); params dma_rx/dma_tx).

Hardware flow control (§49.3.14 MODEM, §49.4.1.6/7, §49.4.2.7) on the UARTn_RTS_b / UARTn_CTS_b pins:
* TXCTSE: before a character moves into the shift register the transmitter samples CTS_b (the pin muxed to
  UARTn_CTS_b; low = asserted). Deasserted: TXD stays idle (the byte waits in the buffer) until CTS_b goes low; a
  change during a character does not affect it. No pin muxed to CTS_b: the input reads asserted (one `info`).
* RXRTSE: RTS_b is asserted (L) while the receive buffer holds fewer than RWFIFO.RXWATER characters and deasserted
  (H) otherwise; characters still arrive until the buffer is full (overrun).
* TXRTSE (transceiver driver enable, RS-485): RTS_b asserts when a character is written to the empty transmitter
  (with TE) and deasserts one bit time after the last stop bit; TXRTSPOL=1 makes it active high. The one-bit lead
  before the start bit is not inserted (the character starts at once).
* None of them: RTS_b stays deasserted (H). The level is driven with PinBoard.drive_signal, so nets and board parts
  see it. Board links still move whole bytes: a sender honours CTS only when its CTS_b pin is wired to the
  receiver's RTS_b (a net).
Low-power modes (`clock_stop`, called by the SMC in STOP/VLPS/LLS/VLLS): the UART is static: a character arriving
is lost (reason stop_mode) and only its start bit counts, as an RX active edge: S2.RXEDGIF (w1c) is set by every
arriving character (start bit falling edge) and, with BDH.RXEDGIE, raises the status interrupt (wakes STOP/VLPS).
A transmission in progress is held and resumes after wake-up.
Not modelled: parity/noise/framing errors, IDLE, break/LIN (LBKDIF never sets), match address, IrDA, ISO-7816, 9-bit
data (C3.T8/R8: board links carry bytes).

Hot path: S1/TCFIFO/RCFIFO/SFIFO are stored values recomputed on every state change (`_refresh`), so polling them
is a plain register read; S1 gets a read hook only while OR is set (the S1-then-D clear sequence).
"""
from __future__ import annotations

from collections import deque
from typing import Any

from ..base import MMIOBlock
from ..uartwire import UartRx
from ..registry import register
from .clockutil import bus_hz, core_hz, irq_number, regs_from_table
from .dma import dma_request

S1_TDRE, S1_TC, S1_RDRF, S1_IDLE, S1_OR = 0x80, 0x40, 0x20, 0x10, 0x08
C2_TIE, C2_TCIE, C2_RIE, C2_TE, C2_RE = 0x80, 0x40, 0x20, 0x08, 0x04
C5_TDMAS, C5_RDMAS = 0x80, 0x20
C1_M, C1_PE = 0x10, 0x02
BDH_SBNS = 0x20
CFIFO_TXFLUSH, CFIFO_RXFLUSH, CFIFO_RXOFE, CFIFO_TXOFE, CFIFO_RXUFE = 0x80, 0x40, 0x04, 0x02, 0x01
PFIFO_TXFE, PFIFO_RXFE = 0x80, 0x08
SFIFO_TXEMPT, SFIFO_RXEMPT, SFIFO_RXOF, SFIFO_TXOF, SFIFO_RXUF = 0x80, 0x40, 0x04, 0x02, 0x01
C3_ORIE = 0x08
S2_LBKDIF, S2_RXEDGIF = 0x80, 0x40
BDH_RXEDGIE = 0x40
MODEM_RXRTSE, MODEM_TXRTSPOL, MODEM_TXRTSE, MODEM_TXCTSE = 0x08, 0x04, 0x02, 0x01

# PFIFO TXFIFOSIZE/RXFIFOSIZE encoding (§49.3.16)
FIFO_SIZES = {1: 0, 4: 1, 8: 2, 16: 3, 32: 4, 64: 5, 128: 6}

_NAMES = ["BDH", "BDL", "C1", "C2", "S1", "S2", "C3", "D", "MA1", "MA2", "C4", "C5", "ED", "MODEM", "IR", None,
          "PFIFO", "CFIFO", "SFIFO", "TWFIFO", "TCFIFO", "RWFIFO", "RCFIFO"]
_RESETS = {"BDL": 0x04, "S1": 0xC0, "SFIFO": 0xC0, "RWFIFO": 0x01}


def _fallback():
    return [(n, i, 1, _RESETS.get(n, 0)) for i, n in enumerate(_NAMES) if n]


@register("kinetis.uart")
class UART(UartRx, MMIOBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        self._rx_init()
        self._txq: deque[int] = deque()
        self._rxq: deque[int] = deque()
        self._shifting = False
        self._tx_ev = None
        self._rx_last = 0
        super().__init__(name, base, size, ram_like=True, log=log, params=params)
        idx = self.params.get("index")
        if idx is None:
            digits = "".join(ch for ch in name if ch.isdigit())
            idx = int(digits) if digits else 0
        self.index = int(idx)
        depth = int(self.params.get("fifo_depth", 1))
        if depth not in FIFO_SIZES:
            raise ValueError(f"{name}: fifo_depth must be one of {sorted(FIFO_SIZES)}")
        self.fifo_size = depth
        enc = FIFO_SIZES[depth]
        ov = {"S1": dict(ro_mask=0xFF),
              "D": dict(on_read=self._d_read, on_write=self._d_write),
              "C1": dict(on_write=self._ctl_write), "C2": dict(on_write=self._c2_write),
              "BDH": dict(on_write=self._c3_write), "BDL": dict(on_write=self._ctl_write),
              "C4": dict(on_write=self._ctl_write), "C5": dict(on_write=self._c5_write),
              "C3": dict(on_write=self._c3_write),
              "S2": dict(w1c_mask=S2_LBKDIF | S2_RXEDGIF, ro_mask=0x01, on_write=self._c3_write),
              "MODEM": dict(ro_mask=0xF0, on_write=self._modem_write),
              "PFIFO": dict(ro_mask=0x77, on_write=self._pfifo_write),
              "CFIFO": dict(ro_mask=0x38, on_write=self._cfifo_write),
              "SFIFO": dict(ro_mask=0xF8, w1c_mask=0x07, on_write=self._sfifo_write),
              "TWFIFO": dict(on_write=self._water_write), "RWFIFO": dict(on_write=self._water_write),
              "TCFIFO": dict(ro_mask=0xFF), "RCFIFO": dict(ro_mask=0xFF)}
        tbl = name if name.startswith("UART") else f"UART{self.index}"
        for r in regs_from_table(tbl, _fallback(), self.params.get("regs_file"), **ov):
            if r.name == "PFIFO":
                r.reset = (enc << 4) | enc
            self.add_reg(r)
        self._s1_reg = self.regs["S1"]
        self._modem_off = self.regs["MODEM"].boff
        self._s2_off = self.regs["S2"].boff
        self._bdh_off = self.regs["BDH"].boff
        self._o = {n: self.regs[n].boff for n in ("S1", "SFIFO", "TCFIFO", "RCFIFO", "TWFIFO", "RWFIFO", "D")}
        self.output = bytearray()
        self.rx_signal = f"UART{self.index}_RX"
        self._or_seen = False
        self._line = False
        self._err_line = False
        self._irq: int | None = None
        self._err_irq: int | None = None
        self._sim = None
        self._warned_te = False
        self._noted_txof = False
        self._in_dma = False
        self.dma_rx = self.dma_tx = -1
        # board links (chipem.board.UartLink): cb(uart, byte) when a byte starts shifting out
        self.tx_listeners: list = []
        self.rts_signal = f"UART{self.index}_RTS_b"
        self.cts_signal = f"UART{self.index}_CTS_b"
        self._cts_pins: list = []
        self._rts_level: str | None = None
        self._txrts_on = False
        self._rts_ev = None
        self._cts_wait = False
        self._cts_noted = False
        self._lp_stopped = False

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irq = irq_number(chip, f"{self.name}_RX_TX") or irq_number(chip, f"UART{self.index}_RX_TX")
        self._err_irq = irq_number(chip, f"{self.name}_ERR") or irq_number(chip, f"UART{self.index}_ERR")
        self._sim = next((b for b in chip.peripherals.values() if getattr(b, "model", None) == "kinetis.sim"), None)
        i = self.index
        rx, tx = (2 + 2 * i, 3 + 2 * i) if i <= 3 else ((10, 10) if i == 4 else (-1, -1))
        self.dma_rx = int(self.params.get("dma_rx", rx))
        self.dma_tx = int(self.params.get("dma_tx", tx))
        pins = getattr(chip, "pins", None)
        if pins is not None:
            self._cts_pins = pins.signal_pins(self.cts_signal)
            for p in self._cts_pins:
                pins.input_listeners[p.id].append(self._cts_changed)
        for b in chip.peripherals.values():
            if getattr(b, "model", None) == "kinetis.dmamux" and self.dma_rx >= 0:
                b.register_source(self.dma_rx, self._rx_dma_active)
                if self.dma_tx != self.dma_rx:
                    b.register_source(self.dma_tx, self._tx_dma_active)

    def reset(self) -> None:
        ev = getattr(self, "_tx_ev", None)
        if ev is not None and self.chip is not None:
            self.chip.sched.cancel(ev)
        self._tx_ev = None
        rev = getattr(self, "_rts_ev", None)
        if rev is not None and self.chip is not None:
            self.chip.sched.cancel(rev)
        self._rts_ev = None
        self._txrts_on = False
        self._cts_wait = False
        self._lp_stopped = False
        self._shifting = False
        self._txq.clear()
        self._rxq.clear()
        self._rx_last = 0
        self._or_seen = False
        if getattr(self, "_line", False):
            self._set_line(False)
        if getattr(self, "_err_line", False):
            self._err_line = False
            if self._err_irq is not None and self.chip is not None:
                self.irq(self._err_irq, False)
        super().reset()
        if hasattr(self, "_o"):
            self._s1_reg.on_read = None
            self._refresh()
            self._update_rts()
        self._wire_reset()
        # the output log survives a reset (the user's view of the wire); the host keeps sending what it has left

    # ------------------------------------------------------------------ timing
    def module_hz(self) -> float:
        if self.chip is None:
            return 0.0
        return core_hz(self.chip) if self.index in (0, 1) else bus_hz(self.chip)

    def sbr(self) -> int:
        return ((self.get("BDH") & 0x1F) << 8) | self.get("BDL")

    def baud(self) -> float:
        sbr = self.sbr()
        if sbr == 0:
            return 0.0
        return self.module_hz() / (16.0 * (sbr + (self.get("C4") & 0x1F) / 32.0))

    def char_bits(self) -> int:
        c1 = self.get("C1")
        bits = 1 + (9 if c1 & C1_M else 8) + 1
        if self.get("BDH") & BDH_SBNS:
            bits += 1
        return bits

    def char_ns(self) -> float:
        b = self.baud()
        return self.char_bits() * 1e9 / b if b > 0 else 0.0

    # ------------------------------------------------------------------ buffers
    def tx_depth(self) -> int:
        return self.fifo_size if self.get("PFIFO") & PFIFO_TXFE else 1

    def rx_depth(self) -> int:
        return self.fifo_size if self.get("PFIFO") & PFIFO_RXFE else 1

    def _refresh(self, keep_flags: bool = False) -> None:
        """Recompute the stored S1/SFIFO/TCFIFO/RCFIFO from the buffers, then the IRQ/DMA lines."""
        mem, o = self.mem, self._o
        txn, rxn = len(self._txq), len(self._rxq)
        old = mem[o["S1"]]
        s1 = old & S1_OR
        if txn <= mem[o["TWFIFO"]] or (keep_flags and old & S1_TDRE):
            s1 |= S1_TDRE
        if not txn and not self._shifting:
            s1 |= S1_TC
        if rxn >= mem[o["RWFIFO"]] or (keep_flags and old & S1_RDRF):
            if not s1 & S1_OR or old & S1_RDRF:        # OR blocks RDRF from asserting (§49.8.4)
                s1 |= S1_RDRF
        mem[o["S1"]] = s1
        sf = mem[o["SFIFO"]] & 0x07
        if not txn:
            sf |= SFIFO_TXEMPT
        if not rxn:
            sf |= SFIFO_RXEMPT
        mem[o["SFIFO"]] = sf
        mem[o["TCFIFO"]] = txn
        mem[o["RCFIFO"]] = rxn
        self._s1_reg.on_read = self._s1_read if s1 & S1_OR else None
        if self._modem_on:
            if self.mem[self._modem_off] & MODEM_TXRTSE:
                self._txrts_check()
            self._update_rts()
        self._update_line()

    # ------------------------------------------------------------------ IRQ
    def _set_line(self, level: bool) -> None:
        if level != self._line:
            self._line = level
            if self._irq is not None and self.chip is not None:
                self.irq(self._irq, level)

    def _update_line(self) -> None:
        s1, c2, c5 = self.get("S1"), self.get("C2"), self.get("C5")
        self._set_line(bool((s1 & S1_TDRE and c2 & C2_TIE and not c5 & C5_TDMAS) or (s1 & S1_TC and c2 & C2_TCIE)
                            or (s1 & S1_RDRF and c2 & C2_RIE and not c5 & C5_RDMAS)
                            or (self.get("S2") & S2_RXEDGIF and self.get("BDH") & BDH_RXEDGIE)))
        sf, cf = self.get("SFIFO"), self.get("CFIFO")
        err = bool((s1 & S1_OR and self.get("C3") & C3_ORIE)
                   or (sf & SFIFO_RXOF and cf & CFIFO_RXOFE) or (sf & SFIFO_TXOF and cf & CFIFO_TXOFE)
                   or (sf & SFIFO_RXUF and cf & CFIFO_RXUFE))
        if err != self._err_line:
            self._err_line = err
            if self._err_irq is not None and self.chip is not None:
                self.irq(self._err_irq, err)
        if self.chip is not None and not self._in_dma:
            # the request stays asserted while the flag holds: let the DMA move bytes until it no longer does
            for _ in range(2 * self.fifo_size + 2):
                if not self._rx_dma_active():
                    break
                n = len(self._rxq)
                if not self.dma_request(self.dma_rx) or len(self._rxq) == n:
                    break
            for _ in range(2 * self.fifo_size + 2):
                if not self._tx_dma_active():
                    break
                n = len(self._txq), len(self.output)
                if not self.dma_request(self.dma_tx) or (len(self._txq), len(self.output)) == n:
                    break

    # ------------------------------------------------------------------ DMA
    def _rx_dma_active(self) -> bool:
        return bool(self.get("S1") & S1_RDRF and self.get("C5") & C5_RDMAS and self.get("C2") & C2_RIE)

    def _tx_dma_active(self) -> bool:
        return bool(self.get("S1") & S1_TDRE and self.get("C5") & C5_TDMAS and self.get("C2") & C2_TIE
                    and self.get("C2") & C2_TE)

    def dma_request(self, source: int) -> bool:
        """Raise DMA request `source` (the DMA, if a channel is routed and enabled, accesses D right away)."""
        if self._in_dma or source < 0:
            return False
        self._in_dma = True
        try:
            return dma_request(self.chip, source)
        finally:
            self._in_dma = False

    # ------------------------------------------------------------------ TX
    def _tx_start(self) -> None:
        """Move buffered bytes into the idle shift register (instantly through all of them without a baud)."""
        while self._txq and not self._shifting and self.get("C2") & C2_TE:
            if self._lp_stopped:
                return
            if self._modem_on and self.get("MODEM") & MODEM_TXCTSE and not self._cts_asserted():
                self._cts_wait = True                  # TXD idles until CTS_b is asserted (§49.4.1.6)
                return
            self._cts_wait = False
            b = self._txq.popleft()
            self.output.append(b)
            self.emit("uart", uart=self.name, dir="tx", byte=b)
            for cb in self.tx_listeners:
                cb(self, b)
            t = self.char_ns()
            if t > 0:
                self._shifting = True
                self._tx_ev = self.chip.sched.in_ns(t, self._tx_done)

    def _tx_done(self) -> None:
        self._tx_ev = None
        self._shifting = False
        self._tx_start()
        self._refresh()

    # ------------------------------------------------------------------ RTS / CTS (MODEM)
    @property
    def _modem_on(self) -> bool:
        return bool(self.mem[self._modem_off]) or self._txrts_on or self._rts_level not in (None, "H")

    def _cts_asserted(self) -> bool:
        pins = self._cts_pins
        sig = self.cts_signal
        for p in pins:
            if p.owner == sig or sig in str(p.owner).split("/"):
                return p.level() == "L"
        if not self._cts_noted:
            self._cts_noted = True
            self.emit("info", block=self.name, msg=f"{self.name}: TXCTSE set but no pin is muxed to {sig}: CTS reads "
                      "asserted")
        return True

    def _cts_changed(self, pin) -> None:
        if self._cts_wait and self._cts_asserted():
            self._tx_start()
            self._refresh()

    def _update_rts(self) -> None:
        m = self.mem[self._modem_off]
        if m & MODEM_RXRTSE:
            lvl = "H" if len(self._rxq) >= self.mem[self._o["RWFIFO"]] else "L"
        elif m & MODEM_TXRTSE:
            active = self._txrts_on
            lvl = ("H" if active else "L") if m & MODEM_TXRTSPOL else ("L" if active else "H")
        else:
            lvl = "H"
        if lvl != self._rts_level:
            self._rts_level = lvl
            pins = getattr(self.chip, "pins", None) if self.chip is not None else None
            if pins is not None:
                pins.drive_signal(self.rts_signal, lvl)

    def _txrts_check(self) -> None:
        """TXRTSE: assert with data in the transmitter, deassert one bit time after the last stop bit."""
        if not self.mem[self._modem_off] & MODEM_TXRTSE:
            return
        busy = bool(self._txq) or self._shifting
        if busy:
            if self._rts_ev is not None:
                self.chip.sched.cancel(self._rts_ev)
                self._rts_ev = None
            if not self._txrts_on and self.get("C2") & C2_TE:
                self._txrts_on = True
                self._update_rts()
        elif self._txrts_on and self._rts_ev is None:
            b = self.baud()
            if b > 0:
                self._rts_ev = self.chip.sched.in_ns(1e9 / b, self._txrts_off)
            else:
                self._txrts_off()

    def _txrts_off(self) -> None:
        self._rts_ev = None
        if not self._txq and not self._shifting:
            self._txrts_on = False
            self._update_rts()

    def _modem_write(self, blk, reg, old, new):
        if not new & MODEM_TXRTSE:
            self._txrts_on = False
        self._update_rts()
        if (old ^ new) & MODEM_TXCTSE or self._cts_wait:
            self._tx_start()
        self._refresh()
        return None

    # ------------------------------------------------------------------ low-power modes
    def clock_stop(self, stopped: bool, mode: str) -> None:
        self._lp_stopped = stopped
        if self.chip is None:
            return
        if stopped:
            if self._tx_ev is not None:
                self._held_tx = self._tx_ev.t_ns - self.chip.sched.now_ns()
                self.chip.sched.cancel(self._tx_ev)
                self._tx_ev = None
            else:
                self._held_tx = None
        else:
            held = getattr(self, "_held_tx", None)
            self._held_tx = None
            if held is not None:
                self._tx_ev = self.chip.sched.in_ns(max(held, 0.0), self._tx_done)
            else:
                self._tx_start()
            self._refresh()

    def _d_write(self, blk, reg, old, new):
        if not self.get("C2") & C2_TE:
            if not self._warned_te:
                self._warned_te = True
                self.emit("warn", block=self.name, msg=f"{self.name}: write to D with transmitter disabled (C2.TE=0)")
            return old
        if len(self._txq) >= self.tx_depth():
            self.mem[self._o["SFIFO"]] |= SFIFO_TXOF          # §49.3.18: byte lost
            if not self._noted_txof:
                self._noted_txof = True
                self.emit("info", block=self.name, msg=f"{self.name}: transmit buffer full, byte 0x{new & 0xFF:02x} "
                          "lost (SFIFO.TXOF)")
        else:
            self._txq.append(new & 0xFF)
            if self.mem[self._modem_off] & MODEM_TXRTSE:
                self._txrts_check()
            self._tx_start()
        self._refresh()
        return old

    # ------------------------------------------------------------------ RX
    def rx_pending(self) -> int:
        return self.wire_pending() + len(self._rxq)

    def _rx_arrive(self, b: int, src: str) -> None:
        if src != "loop":
            self.mem[self._s2_off] |= S2_RXEDGIF      # the start bit is an active edge on RxD (§49.3.6)
        if self._lp_stopped:
            self._rx_drop(b, "stop_mode")
            self._update_line()
            return
        UartRx._rx_arrive(self, b, src)
        if self.mem[self._bdh_off] & BDH_RXEDGIE:
            self._update_line()

    def _rx_block_reason(self) -> str | None:
        sim = self._sim
        if sim is not None and not sim.clock_enabled(self.name if self.name.startswith("UART") else f"UART{self.index}"):
            return "clock_gated"
        if not self.get("C2") & C2_RE:
            return "rx_disabled"
        return None

    def _rx_accept(self, b: int) -> None:
        mem, o = self.mem, self._o
        if mem[o["S1"]] & S1_OR:                       # OR set: nothing is stored, even with room
            self._rx_drop(b, "overrun")
            return
        if len(self._rxq) >= self.rx_depth():
            self._rx_drop(b, "overrun")
            mem[o["S1"]] |= S1_OR
            mem[o["SFIFO"]] |= SFIFO_RXOF
            self._refresh()
            return
        self._rxq.append(b)
        mem[o["D"]] = self._rxq[0]
        self.emit("uart", uart=self.name, dir="rx", byte=b)
        self._refresh()

    def _d_read(self, blk, reg):
        mem, o = self.mem, self._o
        if self._rxq:
            b = self._rx_last = self._rxq.popleft()
        else:
            b = self._rx_last
            mem[o["SFIFO"]] |= SFIFO_RXUF                # §49.3.18: read from an empty buffer
        if self._or_seen and mem[o["S1"]] & S1_OR:
            mem[o["S1"]] &= ~S1_OR                     # S1 read with OR set, then D read
        self._or_seen = False
        mem[o["D"]] = self._rxq[0] if self._rxq else b
        self._refresh()
        return b

    def _s1_read(self, blk, reg):
        self._or_seen = True                           # hook installed only while OR is set
        return None

    # ------------------------------------------------------------------ control
    def _ctl_write(self, blk, reg, old, new):
        return None

    def _c3_write(self, blk, reg, old, new):
        self._update_line()
        return None

    def _c5_write(self, blk, reg, old, new):
        self._update_line()
        return None

    def _c2_write(self, blk, reg, old, new):
        if (new & ~old) & C2_TE and not self._shifting:
            t = self.char_ns()                          # §49.4.1.3: TE 0->1 queues an idle preamble
            if t > 0:
                self._shifting = True
                self._tx_ev = self.chip.sched.in_ns(t, self._tx_done)
        elif new & C2_TE:
            self._tx_start()
        self._refresh()
        return None

    def _pfifo_write(self, blk, reg, old, new):
        self._refresh()
        return None

    def _water_write(self, blk, reg, old, new):
        self._refresh(keep_flags=True)                  # "changing the watermark will not clear TDRE/RDRF"
        return None

    def _cfifo_write(self, blk, reg, old, new):
        if new & CFIFO_RXFLUSH:
            self._rxq.clear()
        if new & CFIFO_TXFLUSH:
            self._txq.clear()
        self._store(reg, new & ~(CFIFO_TXFLUSH | CFIFO_RXFLUSH))
        self._refresh()
        return None

    def _sfifo_write(self, blk, reg, old, new):
        self._refresh()
        return None
