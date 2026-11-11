"""STM32F4 USART/UART (USART1/2/3/6, UART4/5; RM0090 ch. 30), byte-level model.

Baud = f_PCLK / (8 x (2 - OVER8) x USARTDIV), USARTDIV = DIV_Mantissa + DIV_Fraction / (16 or 8)  (BRR[15:4] /
BRR[3:0], with OVER8=1 only BRR[2:0] counts).  PCLK2 for USART1/USART6, PCLK1 for the others (`params.apb`
overrides), taken from the RCC model consistent with the clock the core actually runs at.  BRR = 0: no baud
generator, flags update immediately.  Character time = (1 start + 8|9 data (M; parity included, PCE) + 0.5|1|1.5|2
stop (CR2.STOP)) bits / baud.

TX (RM0090 30.3.2, 30.6.1): TDR + shift register. A character entering the shift register is appended to
`self.output`, logged as a `uart` event {uart, dir: 'tx', byte} and passed to the board-link `tx_listeners`
(cb(uart, byte)) at its start bit. A DR write while the shift register is idle goes straight to it and TXE stays 1
("the data transmission starts, and the TXE bit is immediately set"); while a character (or the idle preamble, or a
break) is shifting the write lands in TDR (TXE = 0) and moves to the shift register at the end of the frame (TXE = 1).
TC is set at the end of a data frame when TXE = 1, and is cleared only by the SR-read-then-DR-write sequence or by
writing 0 (a DR write without a preceding SR read leaves TC set). Setting TE (with UE) first sends an idle frame
(one character time, "Idle characters"; a TE 0-1 pulse sends one after the current character). Writes with UE/TE
off are ignored with one `warn`.
RX (RM0090 sec. 30.3.3 "Character reception", 30.6.1 USART_SR ORE/IDLE; RCC APBxENR/APBxRSTR): no input queue,
like silicon. A character arrives at its stop bit (host input through `feed()` is paced at this USART's character
time, board links call `receive()` at the stop-bit time; see chipem/periph/uartwire.py) and is dropped (`uart` event
dir 'drop', reason) when the RCC clock enable is off (clock_gated), the RCC reset bit holds the USART in reset
(reset), CR1.UE or CR1.RE is clear (rx_disabled) or no pin is in AF mode for USARTn_RX (pin_not_muxed). Otherwise it
goes to RDR: SR.RXNE = 1, logged as dir 'rx'. If RXNE is still set, SR.ORE is set and the new character is lost ("the
RDR content will not be lost ... the shift register will be overwritten"). SR.IDLE is set one character time after
the last character received (once per RXNE; a new character does not clear it). PE/FE/NF/ORE/IDLE clear with the
SR-read-then-DR-read sequence (PE also with SR read then DR write); RXNE/TC (and LBD/CTS) are rc_w0 (writing 0 clears
them); reading DR clears RXNE.
IRQ "<name>" (USART1, UART4 ...) line = (TXE & TXEIE) | (TC & TCIE) | (RXNE & RXNEIE) | (ORE & RXNEIE) | (IDLE &
IDLEIE), level-sensitive.
DMA (RM0090 sec. 30.3.13): CR3.DMAT asserts the DMA request "<name>_TX" (RM0090 Tables 43/44, e.g. USART1_TX on DMA2
stream 7 channel 4) while TXE = 1, so the DMA's DR write (which clears TXE) deasserts it and the next TXE re-asserts it;
CR3.DMAR asserts "<name>_RX" while RXNE = 1 (the DMA's DR read clears RXNE).  Requests go through
`_util.dma_request` to both controllers (chipem/periph/stm32/dma.py).  The TC flag/interrupt path is unchanged (HAL
waits for TC after the DMA transfer-complete interrupt).
Hardware flow control (RM0090 30.3.14; USART1/2/3/6 only, CR3.RTSE/CTSE are reserved on UART4/5): with CTSE a
character written to DR starts only while the USARTn_CTS pin reads L (H or X holds it in TDR with TXE = TC = 0 until
the pin goes low; a character already shifting completes). A CTS pin edge sets SR.CTS (CTSIE interrupt). Without a pin
muxed to USARTn_CTS the input counts as asserted (one `info`). With RTSE the USARTn_RTS pin is driven L while the
receiver can take a character and H while RXNE is set (deasserted at the stop bit that sets RXNE).
Frames (chipem/periph/uartwire.py): characters go out as Frames with their format (M-bit word, with CR1.PCE the
MSB is the parity bit: 7 or 8 data bits + parity (PS), CR2.STOP, the real baud) and the receiver decodes every
arriving frame at its own settings by sample point (30.3.3: samples 8/9/10 of 16, or 4/5/6 of 8 with OVER8, majority
vote; CR3.ONEBIT: one sample, no NF; start bit 3/5/7 then 8/9/10). The word goes to DR with the received parity bit
in its MSB, with PE (parity mismatch, PEIE interrupt), FE (first stop bit 0) and NF (samples disagree) set together
with RXNE (none on overrun). A mismatched baud or word length gives framing / noise errors and garbage as the sample
points dictate; 9-bit words carry bit 8.
Break / LIN (30.3.2, 30.3.8): CR1.SBK sends a break after the character in progress: 10 (M = 0) / 11 (M = 1) low
bits, 13 with CR2.LINEN, then the stop bits; SBK is cleared during the stop bit of the break; CTS is not checked
(30.3.14). It is a `uart` event {dir: 'tx', brk: bits} and a break Frame on links. A received break (all 0
including the stop bit) is a framing error with 0x00 (30.3.3 "Break character") and, with CR2.LINEN, sets SR.LBD
(LBDIE interrupt) when the line stayed low at least 10 (LBDL = 0) / 11 (LBDL = 1) bit times, in mute mode too.
`receive_break(bits)` injects one directly. OVER8 is forced to 0 while LINEN, SCEN or IREN is set (30.6.4).
Half duplex (CR3.HDSEL): the receiver listens on the USARTn_TX pin and receives the characters it transmits (echo).
Mute mode (CR1.RWU, 30.3.6): WAKE = 0 wakes on an idle line: an idle frame detected (one character time) after a
received character, muted ones included, with RWU set before that detection (CR1.RWU note: a byte must have been
received first); IDLE is not set and RWU is cleared when the next character arrives (on silicon at the idle
detection itself). WAKE = 1 on an address mark (MSB of the word = 1 and low 4 bits = CR2.ADD: that character is
received); an address mark that does not match puts the receiver back into mute mode; RWU writes are ignored while
RXNE = 1 in this mode. Muted
characters do not set RXNE. Smartcard (SCEN), IrDA (IREN) and synchronous mode (CR2.CLKEN) are not modelled (one
`warn`; the USART behaves as asynchronous). Stop mode (and Sleep with the RCC LPENR bit clear):
`clock_stop()` freezes the transmitter and drops arriving characters (reason clock_stopped); the F4 USART has no
wake-up from Stop (wake through EXTI on the RX pin).
"""
from __future__ import annotations

from typing import Any

from ..base import MMIOBlock
from ..registry import register
from ..uartwire import SAMPLES_8, SAMPLES_16, START_16, Frame, RxChar, RxFormat, UartRx, decode_frame
from ._util import GatedBlock, apb_hz, build_regs, dma_request, irq_number, signal_pin_level

SR_PE, SR_FE, SR_NF, SR_ORE, SR_IDLE, SR_RXNE, SR_TC, SR_TXE, SR_LBD, SR_CTS = (1 << b for b in range(10))
SR_RC_W0 = SR_RXNE | SR_TC | SR_LBD | SR_CTS
SR_ERR = SR_PE | SR_FE | SR_NF | SR_ORE | SR_IDLE
CR1_SBK, CR1_RWU = 1 << 0, 1 << 1
CR1_RE, CR1_TE, CR1_IDLEIE, CR1_RXNEIE, CR1_TCIE, CR1_TXEIE = 1 << 2, 1 << 3, 1 << 4, 1 << 5, 1 << 6, 1 << 7
CR1_PEIE, CR1_PS, CR1_PCE, CR1_WAKE, CR1_M, CR1_UE, CR1_OVER8 = 1 << 8, 1 << 9, 1 << 10, 1 << 11, 1 << 12, 1 << 13, 1 << 15
CR2_LBDL, CR2_LBDIE, CR2_CLKEN, CR2_LINEN = 1 << 5, 1 << 6, 1 << 11, 1 << 14
CR3_EIE, CR3_IREN, CR3_HDSEL, CR3_SCEN = 1 << 0, 1 << 1, 1 << 3, 1 << 5
CR3_DMAR, CR3_DMAT, CR3_RTSE, CR3_CTSE, CR3_CTSIE, CR3_ONEBIT = 1 << 6, 1 << 7, 1 << 8, 1 << 9, 1 << 10, 1 << 11
START_8 = (1 / 8, 2 / 8)                   # x8 start-bit verification samples (RM0090 Figure 300)
STOP_BITS = (1.0, 0.5, 2.0, 1.5)
APB2_UARTS = ("USART1", "USART6")


@register("stm32.usart")
class USART(UartRx, GatedBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        idx = params.get("index")
        if idx is None:
            digits = "".join(ch for ch in name if ch.isdigit())
            idx = int(digits) if digits else 1
        self.index = int(idx)
        self.kind = str(params.get("kind") or ("uart" if name.startswith("UART") else "usart"))
        self.gate = name
        self.apb = int(params.get("apb", 2 if name in APB2_UARTS else 1))
        ov = {"SR": {"reset": SR_TXE | SR_TC, "ro_mask": 0xFFFFFFFF & ~SR_RC_W0, "on_write": self._sr_write,
                     "on_read": self._sr_read, "on_peek": lambda b, r: None},     # peek: no SR-then-DR clear
              "DR": {"on_read": self._dr_read, "on_write": self._dr_write, "on_peek": lambda b, r: self._rx_byte},
              "BRR": {"on_write": self._ctl_write},
              "CR1": {"on_write": self._cr1_write},
              "CR2": {"on_write": self._ctl_write},
              "CR3": {"on_write": self._cr3_write}}
        tbl = name if name[:-1] in ("USART", "UART") else f"{'UART' if self.kind == 'uart' else 'USART'}{self.index}"
        r = build_regs((name, tbl, "USART1"), size, ov, params.get("regs_file"))
        self._rx_init()
        super().__init__(name, base, size, r, log=log, params=params)
        if "CR3" in self.regs:       # DMAR/DMAT exist on every U(S)ART (RM0090/RM0033 DMA request tables include
            self.regs["CR3"].ro_mask &= ~0xC0      # UART5); STM32F21x.svd omits them for UART5
        self.output = bytearray()
        self.rx_signal = f"{name}_RX"
        self.tx_signal = f"{name}_TX"                    # bit-level waveform on the TX pin (uartwire._tx_emit)
        self._rx_byte = 0
        self._tx_ev = None
        self._idle_ev = None
        self._rst_loc: tuple[int, int] | None = None
        self._line = False
        self._irq: int | None = None
        self._warned_tx = False
        self._sr_seen = False
        self.tx_listeners: list = []           # board links (chipem.board.UartLink): cb(uart, byte)
        self._dma_bits = 0                     # CR3 & (DMAR | DMAT), cached for the flag paths
        self._dma_tx = False                   # request levels last sent to the DMA
        self._dma_rx = False
        self._dma_names = (f"{name}_TX", f"{name}_RX")
        self.cts_signal = f"{name}_CTS"
        self.rts_signal = f"{name}_RTS"
        self._flow = False                     # USART (not UART4/5): RTS/CTS exist
        self._cr3 = 0
        self._tx_hold: int | None = None       # character waiting in TDR for CTS
        self._tx_cur: int | None = None        # character in the shift register (half-duplex echo)
        self._tx_end_ns = 0.0
        self._tx_left_ns: float | None = None  # remaining shift time while the clock is stopped
        self._brk_pending = False
        self._brk_active = False
        self._idle_pending = False             # idle preamble to send (TE set while busy)
        self._tx_data = False                  # the shift register holds a data frame (TC at its end)
        self._sr_seen_tx = False               # SR read since the last DR write (TC clear sequence)
        self._rts_on = False                   # RTS currently driven by us
        self._cts_last: str | None = None
        self._cts_noted = False
        self._stopped = False                  # Stop mode (clock_stop)
        self._last_rx_ns = -1e30
        self._mute_since = 0.0
        self._warned_modes: set[str] = set()
        self.breaks_rx = 0

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irq = irq_number(chip, self.name, f"{'UART' if self.kind == 'uart' else 'USART'}{self.index}")
        self._flow = self.kind != "uart"
        board = getattr(chip, "pins", None)
        if board is not None and self._flow:
            for p in board.signal_pins(self.cts_signal):
                board.input_listeners[p.id].append(self._cts_edge)
        self._rst_loc = None
        rcc_ = self._rcc
        if rcc_ is not None and self._gate_loc is not None:
            from .rcc import _RST_REG, GATES
            gates = getattr(rcc_, "GATES", GATES)            # the RCC's own table (stm32f1.rcc: F1 bit positions)
            enr, bit = gates[self.gate]
            rst = getattr(rcc_, "RST_REGS", _RST_REG).get(enr)
            if rst in rcc_.regs:
                self._rst_loc = (rcc_.regs[rst].offset + bit // 8, 1 << (bit % 8))

    def reset(self) -> None:
        for attr in ("_tx_ev", "_idle_ev"):
            ev = getattr(self, attr, None)
            if ev is not None and self.chip is not None:
                self.chip.sched.cancel(ev)
            setattr(self, attr, None)
        if getattr(self, "_line", False):
            self._set_line(False)
        self._sr_seen = False
        self._sr_seen_tx = False
        super().reset()
        self._dma_bits = 0
        self._dma_update()
        self._wire_reset()
        self._cr3 = 0
        self._tx_hold = self._tx_cur = None
        self._tx_left_ns = None
        self._brk_pending = self._brk_active = False
        self._idle_pending = self._tx_data = False
        self._stopped = False
        self._last_rx_ns = -1e30
        self.rx_signal = f"{self.name}_RX"
        self._rx_pins = None
        if self._rts_on and self.chip is not None and self.chip.pins is not None:
            self.chip.pins.release_signal(self.rts_signal)
        self._rts_on = False
        self._cts_last = None
        # the output log survives a reset (the user's view of the wire); the host keeps sending what it has left

    def write(self, offset: int, size: int, value: int) -> None:
        if self._gate_loc is not None:
            self._gate_ok()
        MMIOBlock.write(self, offset, size, value)

    # ------------------------------------------------------------------ timing
    def module_hz(self) -> float:
        return apb_hz(self.chip, self.apb) if self.chip is not None else 0.0

    def over8(self) -> bool:
        """CR1.OVER8, forced to 0 by hardware when SCEN, IREN or LINEN is set (30.6.4)."""
        return bool(self.get("CR1") & CR1_OVER8) and not (self._cr3 & (CR3_SCEN | CR3_IREN)
                                                          or self.get("CR2") & CR2_LINEN)

    def usartdiv(self) -> float:
        brr = self.get("BRR") & 0xFFFF
        if self.over8():
            return (brr >> 4) + (brr & 7) / 8.0
        return (brr >> 4) + (brr & 0xF) / 16.0

    def baud(self) -> float:
        div = self.usartdiv()
        if div <= 0:
            return 0.0
        over8 = 1 if self.over8() else 0
        return self.module_hz() / (8.0 * (2 - over8) * div)

    def data_bits(self) -> int:
        return 9 if self.get("CR1") & CR1_M else 8

    def stop_bits(self) -> float:
        return STOP_BITS[(self.get("CR2") >> 12) & 3]

    def char_bits(self) -> float:
        bits = 1 + self.data_bits() + self.stop_bits()
        return int(bits) if bits == int(bits) else bits

    def frame(self) -> str:
        """e.g. "8N1", "7E1" (parity takes the MSB of the M-bit word on STM32), "9N2"."""
        cr1 = self.get("CR1")
        d = self.data_bits()
        par = "N"
        if cr1 & CR1_PCE:
            d -= 1
            par = "O" if cr1 & CR1_PS else "E"
        st = self.stop_bits()
        return f"{d}{par}{int(st) if st == int(st) else st}"

    def char_ns(self) -> float:
        b = self.baud()
        return self.char_bits() * 1e9 / b if b > 0 else 0.0

    def _layout(self) -> tuple[int, str]:
        """(data bits, parity) of the M-bit word: with PCE the MSB is the parity bit (RM0090 30.3.1, Table 140)."""
        cr1 = self.get("CR1")
        m = 9 if cr1 & CR1_M else 8
        if cr1 & CR1_PCE:
            return m - 1, "O" if cr1 & CR1_PS else "E"
        return m, "N"

    def tx_frame_for(self, word: int) -> Frame:
        bits, par = self._layout()
        return Frame(word, bits, par, self.stop_bits(), self.baud())

    def rx_format(self) -> RxFormat:
        """Receiver sampling (RM0090 30.3.3): 3 samples at 8/9/10 of 16 (OVER8: 4/5/6 of 8), majority vote, NF when
        they disagree; ONEBIT: one sample in the middle, no NF on data bits. Start bit: samples 3/5/7 then 8/9/10
        (2 of 3 at 0 each). Only the first stop bit is checked."""
        bits, par = self._layout()
        o8 = self.over8()
        if self._cr3 & CR3_ONEBIT:
            smp = (4 / 8,) if o8 else (8 / 16,)
        else:
            smp = SAMPLES_8 if o8 else SAMPLES_16
        return RxFormat(self.baud(), bits, par, self.stop_bits(), samples=smp, start=START_8 if o8 else START_16)

    # ------------------------------------------------------------------ IRQ
    def _set_line(self, level: bool) -> None:
        if level != self._line:
            self._line = level
            if self._irq is not None and self.chip is not None:
                self.irq(self._irq, level)

    def _update_line(self) -> None:
        sr, cr1 = self.get("SR"), self.get("CR1")
        lvl = bool((sr & SR_TXE and cr1 & CR1_TXEIE) or (sr & SR_TC and cr1 & CR1_TCIE)
                   or (sr & (SR_RXNE | SR_ORE) and cr1 & CR1_RXNEIE) or (sr & SR_IDLE and cr1 & CR1_IDLEIE))
        if not lvl and sr & (SR_PE | SR_LBD | SR_CTS | SR_FE | SR_NF | SR_ORE):
            cr3 = self._cr3
            lvl = bool((sr & SR_PE and cr1 & CR1_PEIE) or (sr & SR_LBD and self.get("CR2") & CR2_LBDIE)
                       or (sr & SR_CTS and cr3 & CR3_CTSIE)
                       or (sr & (SR_FE | SR_NF | SR_ORE) and cr3 & CR3_EIE and cr3 & CR3_DMAR))
        self._set_line(lvl)

    def _sr_mod(self, set_bits: int = 0, clr_bits: int = 0) -> None:
        old = self.get("SR")
        self.set("SR", (old | set_bits) & ~clr_bits)
        self._update_line()
        if self._cr3 & CR3_RTSE and (set_bits | clr_bits) & SR_RXNE:
            self._rts_update()
        if self._dma_bits or self._dma_tx or self._dma_rx:
            self._dma_update()

    # ------------------------------------------------------------------ DMA requests
    def _dma_update(self) -> None:
        if self.chip is None:
            return
        sr, d = self.get("SR"), self._dma_bits
        tx = bool(d & CR3_DMAT and sr & SR_TXE)
        rx = bool(d & CR3_DMAR and sr & SR_RXNE)
        if tx != self._dma_tx:
            self._dma_tx = tx
            dma_request(self.chip, self._dma_names[0], tx)
        if rx != self._dma_rx:
            self._dma_rx = rx
            dma_request(self.chip, self._dma_names[1], rx)

    def _cr3_write(self, blk, reg, old, new):
        if not self._flow:
            new &= ~(CR3_RTSE | CR3_CTSE | CR3_CTSIE)       # reserved on UART4/5
        self._store(reg, new)
        self._cr3 = new
        self._dma_bits = new & (CR3_DMAR | CR3_DMAT)
        self._dma_update()
        if (old ^ new) & CR3_HDSEL:
            self.rx_signal = f"{self.name}_TX" if new & CR3_HDSEL else f"{self.name}_RX"
            self._rx_pins = None
        if new & CR3_SCEN:
            self._warn_mode("smartcard")
        if new & CR3_IREN:
            self._warn_mode("IrDA")
        if (old ^ new) & CR3_RTSE:
            self._rts_update()
        if (old ^ new) & CR3_CTSE:
            self._cts_last = self._cts_level()
            self._try_tx_hold()
        self._update_line()
        return new

    def tx_wave_od(self) -> bool:
        """Half duplex (HDSEL) and smartcard (SCEN): one open-drain line (RM0090 30.3.10/30.3.11; 0 pulls low)."""
        return bool(self._cr3 & (CR3_HDSEL | CR3_SCEN))

    def _warn_mode(self, what: str) -> None:
        if what not in self._warned_modes:
            self._warned_modes.add(what)
            self.emit("warn", block=self.name, msg=f"{self.name}: {what} mode is not modelled; the USART behaves as "
                      "a plain asynchronous UART")

    # ------------------------------------------------------------------ hardware flow control
    def _rts_update(self) -> None:
        chip = self.chip
        if chip is None or chip.pins is None:
            return
        if self._cr3 & CR3_RTSE:
            lvl = "H" if self.get("SR") & SR_RXNE else "L"
            self._rts_on = True
            chip.pins.drive_signal(self.rts_signal, lvl)
        elif self._rts_on:
            self._rts_on = False
            chip.pins.release_signal(self.rts_signal)

    def _cts_level(self) -> str:
        lvl = signal_pin_level(self.chip, self.cts_signal) if self.chip is not None else None
        if lvl is None:
            if self._cr3 & CR3_CTSE and not self._cts_noted:
                self._cts_noted = True
                self.emit("info", block=self.name, msg=f"{self.name}: CTSE=1 but no pin is muxed to "
                          f"{self.cts_signal}; nCTS counts as asserted")
            return "L"
        return lvl

    def cts_asserted(self) -> bool:
        return not (self._cr3 & CR3_CTSE) or self._cts_level() == "L"

    def _cts_edge(self, pin) -> None:
        if not self._cr3 & CR3_CTSE:
            return
        lvl = self._cts_level()
        if lvl == self._cts_last:
            return
        self._cts_last = lvl
        self._sr_mod(SR_CTS)
        if lvl == "L":
            self._try_tx_hold()

    def _try_tx_hold(self) -> None:
        if self._tx_hold is not None and self._tx_ev is None and not self._stopped and not self._brk_pending \
                and self.cts_asserted():
            b, self._tx_hold = self._tx_hold, None
            self._start_tx(b)

    # ------------------------------------------------------------------ Stop mode
    def clock_stop(self, stopped: bool, mode: str = "stop") -> None:
        """PWR Stop mode: the APB clock stops. The character in progress freezes (resumes on wake-up); characters
        arriving meanwhile are lost (reason clock_stopped)."""
        if stopped == self._stopped or self.chip is None:
            return
        self._stopped = stopped
        sched = self.chip.sched
        if stopped:
            if self._tx_ev is not None:
                self._tx_left_ns = max(self._tx_end_ns - self.chip.now_ns(), 0.0)
                self._cancel("_tx_ev")
            self._cancel("_idle_ev")
        else:
            if self._tx_left_ns is not None:
                self._tx_end_ns = self.chip.now_ns() + self._tx_left_ns
                self._tx_ev = sched.in_ns(self._tx_left_ns, self._tx_done)
                self._tx_left_ns = None
            self._try_tx_hold()

    def _cancel(self, attr: str) -> None:
        ev = getattr(self, attr)
        if ev is not None:
            self.chip.sched.cancel(ev)
            setattr(self, attr, None)

    # ------------------------------------------------------------------ TX
    def _tx_done(self) -> None:
        self._tx_ev = None
        data, self._tx_data = self._tx_data, False
        if self._brk_active:
            self._brk_active = False
            self.set("CR1", self.get("CR1") & ~CR1_SBK)
        b, self._tx_cur = self._tx_cur, None
        if b is not None and self._cr3 & CR3_HDSEL and self._rx_enabled() and self.tx_frame is not None:
            self._rx_frame(self.tx_frame, "loop")       # single-wire half duplex: the receiver sees the line
        if self._brk_pending:
            self._start_break()
            return
        if self._idle_pending:
            self._start_idle()
            if self._tx_ev is not None:
                return
        if self._tx_hold is not None:
            if self.cts_asserted() and not self._stopped:
                h, self._tx_hold = self._tx_hold, None
                self._start_tx(h)
            return                                     # TXE = 0 (and no TC) while the character waits for CTS
        if data and self.get("SR") & SR_TXE:
            self._sr_mod(SR_TC)                        # frame containing data complete with TXE = 1

    def _dr_write(self, blk, reg, old, new):
        cr1 = self.get("CR1")
        if not (cr1 & CR1_UE and cr1 & CR1_TE):
            if not self._warned_tx:
                self._warned_tx = True
                self.emit("warn", block=self.name, msg=f"{self.name}: write to DR with the transmitter disabled "
                          f"(CR1.UE={int(bool(cr1 & CR1_UE))}, TE={int(bool(cr1 & CR1_TE))})")
            if self._dma_tx:                    # TXE stays 1: the request stays (the DMA acknowledged it)
                dma_request(self.chip, self._dma_names[0], True)
            return old
        if cr1 & CR1_PCE:
            b = new & (0xFF if cr1 & CR1_M else 0x7F)          # the parity bit replaces the MSB
        else:
            b = new & (0x1FF if cr1 & CR1_M else 0xFF)
        clr = SR_TC if self._sr_seen_tx else 0          # TC: SR read followed by a DR write (30.6.1)
        if self._sr_seen and self.get("SR") & SR_PE:
            clr |= SR_PE                                # PE: SR read followed by a DR read or write
        self._sr_seen_tx = False
        if self._tx_ev is not None or self._tx_hold is not None or self._stopped or self._brk_pending \
                or not self.cts_asserted():
            # TDR: waits for the shift register (character, idle preamble or break in progress), CTS or the clock
            # (Stop mode); a second write before the transfer overwrites TDR
            self._tx_hold = b
            self._sr_mod(clr_bits=SR_TXE | clr)
            return old
        if clr:
            self._sr_mod(clr_bits=clr)
        self._start_tx(b)
        return old

    def _start_tx(self, b: int) -> None:
        """TDR (or a direct DR write while idle) -> shift register: TXE is set at once (30.3.2 "the data
        transmission starts, and the TXE bit is immediately set"); TC is set at the end of the frame if TXE is still
        set."""
        self._tx_emit(self.tx_frame_for(b))
        self._cancel("_tx_ev")
        t = self.char_ns()
        self._tx_cur = b & 0x1FF
        self._tx_data = True
        txe_was = self.get("SR") & SR_TXE
        if t > 0:
            self._tx_end_ns = self.chip.now_ns() + t
            self._tx_ev = self.chip.sched.in_ns(t, self._tx_done)
            if txe_was:
                if self._dma_tx:                # TXE stays 1: the next DMA write goes to TDR
                    dma_request(self.chip, self._dma_names[0], True)
            else:
                self._sr_mod(SR_TXE)
        else:
            self._tx_done()
            if txe_was and self._dma_tx:        # no baud generator: TXE never dropped, the request stays
                dma_request(self.chip, self._dma_names[0], True)

    def _start_idle(self) -> None:
        """TE (with UE) set: an idle frame (preamble, one character time of 1s) is sent before the first data
        (30.3.2 "Idle characters", CR1.TE note). TXE stays 1, TC is not affected."""
        self._idle_pending = False
        t = self.char_ns()
        if t <= 0:
            return
        self._tx_cur = None
        self._tx_data = False
        self._tx_end_ns = self.chip.now_ns() + t
        self._tx_ev = self.chip.sched.in_ns(t, self._tx_done)

    def break_low_bits(self) -> int:
        """Low bits of a transmitted break: 13 with CR2.LINEN (30.3.8), else 10 (M = 0) or 11 (M = 1) (30.3.2)."""
        if self.get("CR2") & CR2_LINEN:
            return 13
        return 11 if self.get("CR1") & CR1_M else 10

    def _start_break(self) -> None:
        """Break: break_low_bits() low bits, then the stop bits; SBK is cleared by hardware during the stop bit of
        the break (CR1.SBK, 30.6.4). CTS is not checked for a break (30.3.14 note)."""
        self._brk_pending = False
        self._brk_active = True
        self._tx_cur = None
        self._tx_data = False
        b = self.baud()
        self._tx_emit(Frame.brk_frame(self.break_low_bits(), self.stop_bits(), b))
        low = self.break_low_bits() * 1e9 / b if b > 0 else 0.0
        if low > 0:
            self._tx_end_ns = self.chip.now_ns() + low + self.stop_bits() * 1e9 / b
            self._tx_ev = self.chip.sched.in_ns(low, self._break_stop)
        else:
            self.set("CR1", self.get("CR1") & ~CR1_SBK)
            self._tx_done()

    def _break_stop(self) -> None:
        self.set("CR1", self.get("CR1") & ~CR1_SBK)
        t = self._tx_end_ns - self.chip.now_ns()
        self._tx_ev = self.chip.sched.in_ns(max(t, 0.0), self._tx_done)

    # ------------------------------------------------------------------ RX
    def rx_pending(self) -> int:
        return self.wire_pending() + (1 if self.get("SR") & SR_RXNE else 0)

    def _rx_enabled(self) -> bool:
        cr1 = self.get("CR1")
        return bool(cr1 & CR1_UE and cr1 & CR1_RE)

    def _rx_block_reason(self) -> str | None:
        rcc_ = self._rcc
        if rcc_ is not None:
            loc = self._gate_loc
            if loc is not None and not rcc_.mem[loc[0]] & loc[1]:
                return "clock_gated"
            loc = self._rst_loc
            if loc is not None and rcc_.mem[loc[0]] & loc[1]:
                return "reset"
        if not self._rx_enabled():
            return "rx_disabled"
        if self._stopped:
            return "clock_stopped"
        return None

    def _mute_filter(self, b: int, cr1: int) -> bool:
        """Mute mode (RWU): True if the character is received, False if it is ignored."""
        now = self.chip.now_ns()
        msb = 0x100 if cr1 & CR1_M else 0x80
        if cr1 & CR1_WAKE:                                    # address mark
            if not b & msb:
                return not cr1 & CR1_RWU
            if (b & 0xF) == (self.get("CR2") & 0xF):
                if cr1 & CR1_RWU:
                    self.set("CR1", cr1 & ~CR1_RWU)
                return True
            if not cr1 & CR1_RWU:
                self.set("CR1", cr1 | CR1_RWU)               # another node's address: mute
            return False
        if not cr1 & CR1_RWU:
            return True
        ct = self.char_ns()
        last = self._last_rx_ns
        # wake-up by idle line (30.3.6): an idle frame is detected one character time after a received character
        # (muted ones count); RWU must have been set before that detection ("the USART must first receive a data
        # byte" before RWU, CR1.RWU note)
        if ct > 0 and last > -1e29 and self._mute_since <= last + ct + 1e-6 and now - last >= 2 * ct - 1e-6:
            self.set("CR1", cr1 & ~CR1_RWU)                   # an idle frame preceded this character: wake up
            return True
        return False

    def receive_break(self, bits: int = 13) -> None:
        """A break of `bits` low bit times at this USART's baud ends now (used by tests; links deliver breaks as
        frames): decoded like any character, i.e. FE + RXNE with 0x00 (30.3.3 "Break character") and, with
        CR2.LINEN, LBD when it is at least 10 (LBDL = 0) / 11 (LBDL = 1) bits long (30.3.8)."""
        if self._rx_block_reason() is not None:
            return
        for ch in decode_frame(Frame.brk_frame(bits, self.stop_bits(), self.baud()), self.rx_format()):
            self._rx_char(ch)

    def _rx_accept(self, ch: RxChar) -> None:
        """A decoded character (RM0090 30.3.3): the word goes to RDR with its parity bit in the MSB (PCE) and RXNE,
        together with PE / FE / NF; a break (all 0 including the stop bit) is a framing error with 0x00, plus LBD in
        LIN mode. RXNE still set: ORE, the character is lost. Mute mode (RWU) filters first."""
        self._cancel("_idle_ev")
        cr1 = self.get("CR1")
        bits, par = self._layout()
        word = ch.data
        if par != "N" and ch.pbit is not None:
            word |= ch.pbit << bits
        if ch.brk:
            self.breaks_rx += 1
            cr2 = self.get("CR2")
            if cr2 & CR2_LINEN and ch.brk >= (11 if cr2 & CR2_LBDL else 10) - 0.01:
                self._sr_mod(SR_LBD)                   # LIN break detection works in mute mode too (30.3.8)
        if (cr1 & (CR1_RWU | CR1_WAKE)) and not self._mute_filter(word, cr1):
            self._last_rx_ns = self.chip.now_ns()
            self.emit("uart", uart=self.name, dir="drop", byte=word & 0xFF, dropped=1, reason="mute")
            return
        self._last_rx_ns = self.chip.now_ns()
        if self.get("SR") & SR_RXNE:
            self._rx_drop(word, "overrun")
            self._sr_mod(SR_ORE)
        else:
            self._rx_byte = word
            self.set("DR", word)
            self._rx_event(ch, word)
            err = (SR_FE if ch.fe else 0) | (SR_PE if ch.pe else 0) | (SR_NF if ch.nf else 0)
            self._sr_mod(SR_RXNE | err)
        t = self.char_ns()
        if t > 0:
            self._idle_ev = self.chip.sched.in_ns(t, self._idle)

    def _idle(self) -> None:
        self._idle_ev = None
        if self._rx_enabled() and not self.get("CR1") & CR1_RWU:
            self._sr_mod(SR_IDLE)

    def _dr_read(self, blk, reg):
        sr = self.get("SR")
        clr = 0
        if self._sr_seen and sr & SR_ERR:
            clr |= SR_ERR                       # SR read followed by DR read clears PE/FE/NF/ORE/IDLE
        self._sr_seen = False
        if sr & SR_RXNE:
            self._sr_mod(clr_bits=clr | SR_RXNE)
        elif clr:
            self._sr_mod(clr_bits=clr)
        return self._rx_byte

    def _sr_read(self, blk, reg):
        self._sr_seen = True
        self._sr_seen_tx = True
        return None

    def _sr_write(self, blk, reg, old, new):
        v = (old & ~SR_RC_W0) | (old & new & SR_RC_W0)        # rc_w0: writing 0 clears, 1 keeps
        self._store(reg, v)
        self._update_line()
        if self._dma_rx:
            self._dma_update()
        return v

    # ------------------------------------------------------------------ control
    def _ctl_write(self, blk, reg, old, new):
        if reg.name == "CR2":
            if new & CR2_CLKEN:
                self._warn_mode("synchronous (CR2.CLKEN)")
            self._store(reg, new)
            self._update_line()
        return None

    def _cr1_write(self, blk, reg, old, new):
        if (old ^ new) & CR1_RWU and new & CR1_WAKE and self.get("SR") & SR_RXNE:
            new = (new & ~CR1_RWU) | (old & CR1_RWU)   # address-mark mode: RWU not writable while RXNE = 1 (30.3.6)
        self._store(reg, new)
        if not (new & CR1_UE and new & CR1_RE):
            self._cancel("_idle_ev")
        if new & ~old & CR1_RWU:
            self._mute_since = self.chip.now_ns() if self.chip is not None else 0.0
        if new & ~old & CR1_SBK and new & CR1_UE and new & CR1_TE and not self._brk_active:
            self._brk_pending = True
            if self._tx_ev is None:
                self._start_break()
        if not (new & CR1_UE and new & CR1_TE):
            self._tx_hold = None
        elif not (old & CR1_UE and old & CR1_TE) and self.chip is not None:
            # transmitter enabled: an idle frame goes out before the first character (after the current one)
            if self._tx_ev is None and not self._stopped:
                self._start_idle()
            else:
                self._idle_pending = True
        self._update_line()
        return new
