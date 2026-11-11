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
Low-power modes (`clock_stop`, called by the SMC in STOP/VLPS/LLS/VLLS, and in WAIT/VLPW with C1.UARTSWAI): the
UART is static: a character arriving is lost (reason stop_mode) and only its start bit counts, as an RX active edge:
S2.RXEDGIF (w1c) is set by every arriving character (start bit falling edge) and, with BDH.RXEDGIE, raises the status
interrupt (wakes STOP/VLPS). A transmission in progress and the idle-line count are held and resume after wake-up.

Frames (chipem/periph/uartwire.py): every character goes out as a Frame with its real format: C1.M / PE / PT and
C4.M10 (§49.4.4 Tables 49-244/245: 8-bit mode with PE = 7 data + parity, 9-bit mode (C3.T8 latched with the D
write) with PE = 8 data + parity, M10 = 9 data + parity), BDH.SBNS stop bits, S2.MSBF bit order and C3.TXINV
polarity. C2.SBK sends break characters (Table 49-237: 10/11/12 or, with S2.BRK13, 13/14 low bits, then a logic 1;
repeated while SBK stays set; a queued break preempts the buffered data; TC = 0 meanwhile).
The receiver decodes every arriving frame at its own settings (16 RT samples per bit, start verified at RT3/5/7,
RT8/9/10 majority with the noise flag, resynchronisation on falling data edges, S2.MSBF / RXINV) and sets, with the
dataword, S1.NF / FE / PF (none on overrun) and ED.NOISY / PARITYE per buffered dataword; D (8-bit mode) or C3.R8
(9-bit mode) hold the received parity bit; C3.R8 is the ninth bit of the dataword at the head of the buffer.
S1.FE inhibits further reception until cleared (reason framing_error). A break (all 0 including the stop bit) is a
framing error with an all-0 dataword; with S2.LBKDE, 11 (M = 0) / 12 (M = 1) low bits set S2.LBKDIF (BDH.LBKDIE
status interrupt) instead, RDRF/FE/NF/PF cannot set, datawords overwrite each other and OR sets when a character
arrives while LBKDIF is still set. S1.IDLE: 10/11/12 consecutive 1s counted after the start bit (C1.ILT = 0; the
character's trailing 1s count) or after the stop bit (ILT = 1), once per received frame, blocked while OR is set,
C2.ILIE status interrupt; it is flagged when the next frame shows the line was idle long enough, or one character
time after the idle time (the receiver only learns about a frame at its stop bit). OR, IDLE, NF, FE, PF clear by
reading S1 (flag set) and then D. Error interrupt "UART<n>_ERR" adds (NF & C3.NEIE) | (FE & FEIE) | (PF & PEIE).
Receiver wake-up (§49.4.2.10): C2.RWU with C1.WAKE = 0 ignores characters until an idle character (it clears RWU;
IDLE only with S2.RWUID), WAKE = 1 until a frame with the address mark (the bit before the stop bit) = 1, which is
received; ignored characters set no flags (reason mute). Match address (C4.MAEN1/MAEN2 with MA1/MA2): a marked
address that matches is received with the data that follows; others and their data are discarded (reason
address_mismatch).
Not modelled: S2.RAF, C1.LOOPS/RSRC (loop and single-wire modes), IrDA (IR), ISO-7816, a receiver started in the
middle of a character.

Hot path: S1/TCFIFO/RCFIFO/SFIFO are stored values recomputed on every state change (`_refresh`), so polling them
is a plain register read; S1 gets a read hook only while OR is set (the S1-then-D clear sequence).
"""
from __future__ import annotations

from collections import deque
from typing import Any

from ..base import MMIOBlock
from ..uartwire import SAMPLES_16, START_16, Frame, RxChar, RxFormat, UartRx
from ..registry import register
from .clockutil import bus_hz, core_hz, irq_number, regs_from_table
from .dma import dma_request

S1_TDRE, S1_TC, S1_RDRF, S1_IDLE, S1_OR, S1_NF, S1_FE, S1_PF = 0x80, 0x40, 0x20, 0x10, 0x08, 0x04, 0x02, 0x01
S1_RXERR = S1_OR | S1_NF | S1_FE | S1_PF
S1_CLR = S1_IDLE | S1_RXERR                 # cleared by reading S1 (flag set) and then D (§49.3.5)
C2_TIE, C2_TCIE, C2_RIE, C2_ILIE, C2_TE, C2_RE, C2_RWU, C2_SBK = 0x80, 0x40, 0x20, 0x10, 0x08, 0x04, 0x02, 0x01
C5_TDMAS, C5_RDMAS = 0x80, 0x20
C1_M, C1_WAKE, C1_ILT, C1_PE, C1_PT = 0x10, 0x08, 0x04, 0x02, 0x01
BDH_LBKDIE, BDH_SBNS = 0x80, 0x20
C3_R8, C3_T8, C3_TXINV, C3_NEIE, C3_FEIE, C3_PEIE = 0x80, 0x40, 0x10, 0x04, 0x02, 0x01
C4_MAEN1, C4_MAEN2, C4_M10 = 0x80, 0x40, 0x20
S2_MSBF, S2_RXINV, S2_RWUID, S2_BRK13, S2_LBKDE = 0x20, 0x10, 0x08, 0x04, 0x02
ED_NOISY, ED_PARITYE = 0x80, 0x40
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
        self._rxed: deque[int] = deque()       # ED flags (NOISY/PARITYE) of each buffered dataword
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
              "D": dict(on_read=self._d_read, on_write=self._d_write, on_peek=self._d_peek),
              "C1": dict(on_write=self._ctl_write), "C2": dict(on_write=self._c2_write),
              "BDH": dict(on_write=self._c3_write), "BDL": dict(on_write=self._ctl_write),
              "C4": dict(on_write=self._ctl_write), "C5": dict(on_write=self._c5_write),
              "C3": dict(on_write=self._c3_write, ro_mask=C3_R8),
              "ED": dict(ro_mask=0xFF),
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
        self._o = {n: self.regs[n].boff for n in ("S1", "SFIFO", "TCFIFO", "RCFIFO", "TWFIFO", "RWFIFO", "D", "C3",
                                                  "ED", "C2")}
        self.output = bytearray()
        self.rx_signal = f"UART{self.index}_RX"
        self.tx_signal = f"UART{self.index}_TX"          # bit-level waveform on the TX pin (uartwire._tx_emit)
        self._s1_seen = 0                      # S1 flags seen set by an S1 read (cleared by the next D read)
        self._idle_ev = None
        self._idle_at = 0.0                    # when the idle character completes (if no frame starts before)
        self._idle_armed = False               # a frame arrived since IDLE was last set (§49.3.5 IDLE)
        self._idle_left: float | None = None   # idle-count time left while the clock is stopped
        self._brk_pending = False              # SBK: a break preempts the queued data (§49.4.1.4)
        self._brk_active = False
        self._ma_ok = False                    # match-address mode: the last marked address matched
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
        iev = getattr(self, "_idle_ev", None)
        if iev is not None and self.chip is not None:
            self.chip.sched.cancel(iev)
        self._idle_ev = None
        self._idle_armed = False
        self._idle_left = None
        self._brk_pending = self._brk_active = False
        self._ma_ok = False
        self._txrts_on = False
        self._cts_wait = False
        self._lp_stopped = False
        self._shifting = False
        self._txq.clear()
        self._rxq.clear()
        self._rxed.clear()
        self._rx_last = 0
        self._s1_seen = 0
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
        bits, par = self._layout()
        return 1 + bits + (par != "N") + (2 if self.get("BDH") & BDH_SBNS else 1)

    def _layout(self) -> tuple[int, str]:
        """(data bits, parity) from C1.M/PE/PT and C4.M10 (§49.4.4 Tables 49-244/245): the parity bit is the bit
        before the stop bit, so 8-bit mode with PE has 7 data bits, 9-bit mode with PE 8, and M10 9 + parity."""
        c1 = self.get("C1")
        pe = c1 & C1_PE
        if c1 & C1_M:
            bits = 9 if (not pe or self.get("C4") & C4_M10) else 8
        else:
            bits = 7 if pe else 8
        return bits, (("O" if c1 & C1_PT else "E") if pe else "N")

    def stop_bits(self) -> float:
        return 2.0 if self.get("BDH") & BDH_SBNS else 1.0

    def frame(self) -> str:
        bits, par = self._layout()
        return f"{bits}{par}{int(self.stop_bits())}"

    def tx_frame_for(self, word: int) -> Frame:
        bits, par = self._layout()
        return Frame(word, bits, par, self.stop_bits(), self.baud(), msbf=bool(self.get("S2") & S2_MSBF),
                     inv=bool(self.get("C3") & C3_TXINV))

    def rx_format(self) -> RxFormat:
        """16 RT samples per bit (§49.4.2.4): start verified at RT3/5/7, data and stop bits by RT8/9/10 majority (noise
        flag when they differ), RT clock resynchronised on falling data edges; S2.MSBF / RXINV."""
        bits, par = self._layout()
        s2 = self.get("S2")
        return RxFormat(self.baud(), bits, par, self.stop_bits(), msbf=bool(s2 & S2_MSBF), inv=bool(s2 & S2_RXINV),
                        samples=SAMPLES_16, start=START_16, resync=True)

    def break_bits(self) -> int:
        """Transmitted break length (Table 49-237): BRK13 = 0: 10 (M = 0), 11 (M = 1), 12 (M = 1, M10 and PE);
        BRK13 = 1: 13 (M = 0), 14 (M = 1)."""
        c1 = self.get("C1")
        if self.get("S2") & S2_BRK13:
            return 14 if c1 & C1_M else 13
        if not c1 & C1_M:
            return 10
        return 12 if self.get("C4") & C4_M10 and c1 & C1_PE else 11

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
        s1 = old & S1_CLR
        if txn <= mem[o["TWFIFO"]] or (keep_flags and old & S1_TDRE):
            s1 |= S1_TDRE
        if not txn and not self._shifting:
            s1 |= S1_TC
        if rxn >= mem[o["RWFIFO"]] or (keep_flags and old & S1_RDRF):
            if old & S1_RDRF or not (s1 & S1_OR or mem[self._s2_off] & S2_LBKDE):
                s1 |= S1_RDRF                          # OR / LBKDE block RDRF from asserting (§49.8.4, S2.LBKDE)
        mem[o["S1"]] = s1
        if rxn:
            head = self._rxq[0]
            mem[o["ED"]] = self._rxed[0]
            if (mem[o["C3"]] & C3_R8) != (C3_R8 if head & 0x100 else 0):
                mem[o["C3"]] ^= C3_R8
        else:
            mem[o["ED"]] = 0
        sf = mem[o["SFIFO"]] & 0x07
        if not txn:
            sf |= SFIFO_TXEMPT
        if not rxn:
            sf |= SFIFO_RXEMPT
        mem[o["SFIFO"]] = sf
        mem[o["TCFIFO"]] = txn
        mem[o["RCFIFO"]] = rxn
        self._s1_reg.on_read = self._s1_read if s1 & S1_CLR else None
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
        s2, bdh = self.get("S2"), self.get("BDH")
        self._set_line(bool((s1 & S1_TDRE and c2 & C2_TIE and not c5 & C5_TDMAS) or (s1 & S1_TC and c2 & C2_TCIE)
                            or (s1 & S1_RDRF and c2 & C2_RIE and not c5 & C5_RDMAS) or (s1 & S1_IDLE and c2 & C2_ILIE)
                            or (s2 & S2_RXEDGIF and bdh & BDH_RXEDGIE) or (s2 & S2_LBKDIF and bdh & BDH_LBKDIE)))
        sf, cf = self.get("SFIFO"), self.get("CFIFO")
        c3 = self.get("C3")
        err = bool((s1 & S1_OR and c3 & C3_ORIE) or (s1 & S1_NF and c3 & C3_NEIE) or (s1 & S1_FE and c3 & C3_FEIE)
                   or (s1 & S1_PF and c3 & C3_PEIE)
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
        """Move buffered bytes into the idle shift register (instantly through all of them without a baud). A
        queued break goes first (it preempts the buffered data, §49.4.1.4 note)."""
        while (self._txq or self._brk_pending) and not self._shifting and self.get("C2") & C2_TE:
            if self._lp_stopped:
                return
            if self._brk_pending:
                self._start_break()
                continue
            if self._modem_on and self.get("MODEM") & MODEM_TXCTSE and not self._cts_asserted():
                self._cts_wait = True                  # TXD idles until CTS_b is asserted (§49.4.1.6)
                return
            self._cts_wait = False
            b = self._txq.popleft()
            self._tx_emit(self.tx_frame_for(b))
            t = self.char_ns()
            if t > 0:
                self._shifting = True
                self._tx_ev = self.chip.sched.in_ns(t, self._tx_done)

    def _start_break(self) -> None:
        """C2.SBK: a break character of break_bits() logic 0s (no start/stop/parity bit), followed by at least one
        logic 1 (modelled as one stop bit); while SBK stays set, breaks repeat (§49.4.1.4, C2.SBK)."""
        self._brk_pending = False
        self._brk_active = True
        b = self.baud()
        fr = Frame.brk_frame(self.break_bits(), 1.0, b, inv=bool(self.get("C3") & C3_TXINV))
        self._tx_emit(fr)
        t = fr.duration_ns()
        if t > 0:
            self._shifting = True
            self._tx_ev = self.chip.sched.in_ns(t, self._tx_done)
        else:
            self._brk_active = False

    def _tx_done(self) -> None:
        self._tx_ev = None
        self._shifting = False
        if self._brk_active:
            self._brk_active = False
            if self.get("C2") & C2_SBK and self.get("C2") & C2_TE:
                self._brk_pending = True               # SBK still set: the next break character
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
            if self._idle_ev is not None:
                self._idle_left = max(self._idle_ev.t_ns - self.chip.sched.now_ns(), 0.0)
                self.chip.sched.cancel(self._idle_ev)
                self._idle_ev = None
            if self._ln is not None:
                self._ln.reset()
        else:
            if self._idle_left is not None:
                self._idle_ev = self.chip.sched.in_ns(self._idle_left, self._idle_fire)
                self._idle_left = None
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
            # 9-bit formats: C3.T8 is the ninth bit, taken with the D write (§49.3.7, §49.3.8 note)
            self._txq.append((new & 0xFF) | (0x100 if self.mem[self._o["C3"]] & C3_T8 else 0))
            if self.mem[self._modem_off] & MODEM_TXRTSE:
                self._txrts_check()
            self._tx_start()
        self._refresh()
        return old

    # ------------------------------------------------------------------ RX
    def rx_pending(self) -> int:
        return self.wire_pending() + len(self._rxq)

    def _rx_frame(self, fr: Frame, src: str, t_end: float | None = None) -> None:
        if src != "loop":
            self.mem[self._s2_off] |= S2_RXEDGIF      # the start bit is an active edge on RxD (§49.3.6)
        if self._lp_stopped:
            self._rx_drop(fr.data if not fr.brk else 0, "stop_mode")
            self._update_line()
            return
        if self._idle_ev is not None:
            # the idle check waits one character past the idle time so that a frame which started before it (and
            # is delivered at its stop bit) can still cancel it: was the line idle long enough before this start?
            self.chip.sched.cancel(self._idle_ev)
            self._idle_ev = None
            t_start = (self.chip.now_ns() if t_end is None else t_end) - fr.duration_ns()
            if t_start >= self._idle_at - 1.0:
                self._idle_fire()
        UartRx._rx_frame(self, fr, src, t_end)
        if self.mem[self._bdh_off] & BDH_RXEDGIE:
            self._update_line()

    def _rx_block_reason(self) -> str | None:
        sim = self._sim
        if sim is not None and not sim.clock_enabled(self.name if self.name.startswith("UART") else f"UART{self.index}"):
            return "clock_gated"
        if not self.get("C2") & C2_RE:
            return "rx_disabled"
        return None

    def _idle_after(self, ch: RxChar) -> None:
        """Schedule the idle-line detection after this character: 10/11/12 consecutive 1s (M, M10&PE), counted from
        after the start bit (ILT = 0: the character's trailing 1s count) or after the stop bit (ILT = 1)."""
        rx_bits, par = self._layout()
        n = rx_bits + (par != "N")
        need = n + 2
        b = self.baud()
        if b <= 0:
            return
        if not self.get("C1") & C1_ILT:
            cells = ch.data | ((ch.pbit or 0) << rx_bits) if par != "N" else ch.data
            trail = 1                                  # the stop bit
            for i in range(n - 1, -1, -1):
                if (cells >> i) & 1 and not ch.fe:
                    trail += 1
                else:
                    break
            need -= trail if not ch.fe else 0
        now = self.chip.now_ns()
        self._idle_at = now + max(need, 0) * 1e9 / b
        self._idle_ev = self.chip.sched.at_ns(self._idle_at + self.char_ns(), self._idle_fire)

    def _idle_fire(self) -> None:
        """Idle line (S1.IDLE, §49.3.5): set once per received frame, blocked while OR is set; with RWU and WAKE = 0
        the idle character wakes the receiver (clears RWU) and sets IDLE only with S2.RWUID (§49.4.2.10.1)."""
        self._idle_ev = None
        if not self._idle_armed or not self.get("C2") & C2_RE:
            return
        self._idle_armed = False
        mem, o = self.mem, self._o
        c2 = mem[o["C2"]]
        if c2 & C2_RWU and not self.get("C1") & C1_WAKE:
            mem[o["C2"]] = c2 & ~C2_RWU
            if not mem[self._s2_off] & S2_RWUID:
                return
        if not mem[o["S1"]] & S1_OR:
            mem[o["S1"]] |= S1_IDLE
            self._refresh()

    def _rx_accept(self, ch: RxChar) -> None:
        """A decoded character (§49.4.2.3-49.4.2.10): wake-up / match address, LIN break detection, error flags
        (NF, FE, PF set with the dataword; none on overrun), buffer and overrun."""
        mem, o = self.mem, self._o
        c1, c2, s2 = self.get("C1"), mem[o["C2"]], mem[self._s2_off]
        bits, par = self._layout()
        word = ch.data
        if par != "N" and ch.pbit is not None and bits < 9:
            word |= ch.pbit << bits                    # D (8-bit) / R8 (9-bit) hold the received parity bit
        mark = (ch.pbit if par != "N" else (ch.data >> (bits - 1)) & 1) or 0   # bit before the stop bit
        self._idle_armed = True
        self._idle_after(ch)
        if s2 & S2_LBKDE:
            # LIN break detection (S2.LBKDE): 11 (M = 0) / 12 (M = 1) low bits set LBKDIF; RDRF/FE/NF/PF cannot set;
            # datawords overwrite each other; OR if LBKDIF is still set when the next character arrives
            if ch.brk and ch.brk >= (12 if c1 & C1_M else 11) - 0.01:
                mem[self._s2_off] |= S2_LBKDIF
                self.emit("uart", uart=self.name, dir="rx", brk=round(ch.brk, 1), lin=1)
                self._refresh()
                return
            if s2 & S2_LBKDIF:
                self._rx_drop(word, "overrun")
                mem[o["S1"]] |= S1_OR
                self._refresh()
                return
            if self._rxq:
                self._rxq[-1] = word
                self._rxed[-1] = 0
            else:
                self._rxq.append(word)
                self._rxed.append(0)
            self._rx_event(RxChar(ch.data, ch.addr, ch.pbit), word)
            self._refresh()
            return
        if c1 & C1_WAKE:
            if c2 & C2_RWU:
                if not mark:
                    self._rx_drop(word, "mute")        # standby: no flags (§49.4.2.10.2)
                    return
                mem[o["C2"]] = c2 & ~C2_RWU            # an address mark wakes the receiver
        elif c2 & C2_RWU:
            self._rx_drop(word, "mute")                # idle-line wake-up pending
            return
        c4 = self.get("C4")
        if c4 & (C4_MAEN1 | C4_MAEN2):
            if mark:
                a = word & 0xFF
                self._ma_ok = bool((c4 & C4_MAEN1 and a == self.get("MA1")) or (c4 & C4_MAEN2 and a == self.get("MA2")))
            if not self._ma_ok:
                self._rx_drop(word, "address_mismatch")
                return
        s1 = mem[o["S1"]]
        if s1 & S1_FE:
            self._rx_drop(word, "framing_error")       # "FE inhibits further data reception until it is cleared"
            return
        if s1 & S1_OR:                                 # OR set: nothing is stored, even with room
            self._rx_drop(word, "overrun")
            return
        if len(self._rxq) >= self.rx_depth():
            self._rx_drop(word, "overrun")
            mem[o["S1"]] |= S1_OR
            mem[o["SFIFO"]] |= SFIFO_RXOF
            self._refresh()
            return
        self._rxq.append(word)
        self._rxed.append((ED_NOISY if ch.nf else 0) | (ED_PARITYE if ch.pe else 0))
        mem[o["D"]] = self._rxq[0] & 0xFF
        mem[o["S1"]] |= (S1_NF if ch.nf else 0) | (S1_FE if ch.fe else 0) | (S1_PF if ch.pe else 0)
        self._rx_event(ch, word)
        self._refresh()

    def _d_read(self, blk, reg):
        mem, o = self.mem, self._o
        if self._rxq:
            b = self._rx_last = self._rxq.popleft()
            self._rxed.popleft()
        else:
            b = self._rx_last
            mem[o["SFIFO"]] |= SFIFO_RXUF                # §49.3.18: read from an empty buffer
        seen = self._s1_seen & mem[o["S1"]]
        if seen:
            mem[o["S1"]] &= ~seen                      # S1 read with the flag set, then D read
        self._s1_seen = 0
        mem[o["D"]] = (self._rxq[0] if self._rxq else b) & 0xFF
        self._refresh()
        return b & 0xFF

    def _d_peek(self, blk, reg):
        return (self._rxq[0] if self._rxq else self._rx_last) & 0xFF     # the FIFO head, not popped

    def _s1_read(self, blk, reg):
        self._s1_seen |= self.mem[self._o["S1"]] & S1_CLR     # hook installed only while one of them is set
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
        if (new & ~old) & C2_SBK and new & C2_TE and not self._brk_active:
            self._brk_pending = True                   # one break character per SBK set (more while it stays set)
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
            self._rxed.clear()
        if new & CFIFO_TXFLUSH:
            self._txq.clear()
        self._store(reg, new & ~(CFIFO_TXFLUSH | CFIFO_RXFLUSH))
        self._refresh()
        return None

    def _sfifo_write(self, blk, reg, old, new):
        self._refresh()
        return None
