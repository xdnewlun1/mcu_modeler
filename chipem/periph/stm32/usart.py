"""STM32F4 USART/UART (USART1/2/3/6, UART4/5; RM0090 ch. 30), byte-level model.

Baud = f_PCLK / (8 x (2 - OVER8) x USARTDIV), USARTDIV = DIV_Mantissa + DIV_Fraction / (16 or 8)  (BRR[15:4] /
BRR[3:0], with OVER8=1 only BRR[2:0] counts).  PCLK2 for USART1/USART6, PCLK1 for the others (`params.apb`
overrides), taken from the RCC model consistent with the clock the core actually runs at.  BRR = 0: no baud
generator, flags update immediately.  Character time = (1 start + 8|9 data (M; parity included, PCE) + 0.5|1|1.5|2
stop (CR2.STOP)) bits / baud.

TX: writing DR with CR1.UE and CR1.TE set appends the byte to `self.output`, logs a `uart` event {uart, dir: 'tx',
byte} and calls the board-link `tx_listeners` (cb(uart, byte)); SR.TXE and SR.TC clear and are set again one
character time later (single-buffered, like kinetis.uart, so link timing is exact).  Writes with UE/TE off are
ignored with one `warn`.
RX (RM0090 sec. 30.3.3 "Character reception", 30.6.1 USART_SR ORE/IDLE; RCC APBxENR/APBxRSTR): no input queue,
like silicon. A character arrives at its stop bit (host input through `feed()` is paced at this USART's character
time, board links call `receive()` at the stop-bit time; see chipem/periph/uartwire.py) and is dropped (`uart` event
dir 'drop', reason) when the RCC clock enable is off (clock_gated), the RCC reset bit holds the USART in reset
(reset), CR1.UE or CR1.RE is clear (rx_disabled) or no pin is in AF mode for USARTn_RX (pin_not_muxed). Otherwise it
goes to RDR: SR.RXNE = 1, logged as dir 'rx'. If RXNE is still set, SR.ORE is set and the new character is lost ("the
RDR content will not be lost ... the shift register will be overwritten"). SR.IDLE is set one character time after
the last character received. PE/FE/NF/ORE/IDLE clear with the SR-read-then-DR-read sequence; RXNE/TC (and LBD/CTS)
are rc_w0 (writing 0 clears them); reading DR clears RXNE.
IRQ "<name>" (USART1, UART4 ...) line = (TXE & TXEIE) | (TC & TCIE) | (RXNE & RXNEIE) | (ORE & RXNEIE) | (IDLE &
IDLEIE), level-sensitive.
DMA (RM0090 sec. 30.3.13): CR3.DMAT asserts the DMA request "<name>_TX" (RM0090 Tables 42/43, e.g. USART1_TX on DMA2
stream 7 channel 4) while TXE = 1, so the DMA's DR write (which clears TXE) deasserts it and the next TXE re-asserts it;
CR3.DMAR asserts "<name>_RX" while RXNE = 1 (the DMA's DR read clears RXNE).  Requests go through
`_util.dma_request` to both controllers (chipem/periph/stm32/dma.py).  The TC flag/interrupt path is unchanged (HAL
waits for TC after the DMA transfer-complete interrupt).
Hardware flow control (RM0090 30.3.14; USART1/2/3/6 only, CR3.RTSE/CTSE are reserved on UART4/5): with CTSE a
character written to DR starts only while the USARTn_CTS pin reads L (H or X holds it in TDR with TXE = TC = 0 until
the pin goes low; a character already shifting completes). A CTS pin edge sets SR.CTS (CTSIE interrupt). Without a pin
muxed to USARTn_CTS the input counts as asserted (one `info`). With RTSE the USARTn_RTS pin is driven L while the
receiver can take a character and H while RXNE is set (deasserted at the stop bit that sets RXNE).
Parity (CR1.PCE, PS): the parity bit takes the word's MSB, so 7 (M=0) or 8 (M=1) data bits go on the link; a received
character gets its computed parity bit in the MSB of DR, as silicon returns it. PE is never set (links carry no
parity errors). 9-bit words (M=1, PCE=0): board links and the host carry 8 bits, bit 8 is sent as 0 / received as 0.
Break / LIN (30.3.11): CR1.SBK sends a break (data bits + 1 low bits, then the stop bits; SBK clears at its end, after
the character in progress); it is logged as a `uart` event {dir: 'tx', brk: 1} but not carried by board links.
`receive_break()` delivers a break to the receiver: FE + RXNE with 0x00, and SR.LBD (LBDIE interrupt) when
CR2.LINEN = 1.
Half duplex (CR3.HDSEL): the receiver listens on the USARTn_TX pin and receives the characters it transmits (echo).
Mute mode (CR1.RWU, 30.3.8): WAKE = 0 wakes on an idle line (at least one idle character time before the arriving
character; IDLE is not set in mute mode), WAKE = 1 on an address mark (MSB of the word = 1 and low 4 bits = CR2.ADD:
that character is received); an address mark that does not match puts the receiver back into mute mode. Muted
characters do not set RXNE. Smartcard (SCEN), IrDA (IREN) and synchronous mode (CR2.CLKEN) are not modelled (one
`warn`; the USART behaves as asynchronous). Stop mode: `clock_stop()` freezes the transmitter and drops arriving
characters (reason clock_stopped).
"""
from __future__ import annotations

from typing import Any

from ..base import MMIOBlock
from ..registry import register
from ..uartwire import UartRx
from ._util import GatedBlock, apb_hz, build_regs, dma_request, irq_number, signal_pin_level

SR_PE, SR_FE, SR_NF, SR_ORE, SR_IDLE, SR_RXNE, SR_TC, SR_TXE, SR_LBD, SR_CTS = (1 << b for b in range(10))
SR_RC_W0 = SR_RXNE | SR_TC | SR_LBD | SR_CTS
SR_ERR = SR_PE | SR_FE | SR_NF | SR_ORE | SR_IDLE
CR1_SBK, CR1_RWU = 1 << 0, 1 << 1
CR1_RE, CR1_TE, CR1_IDLEIE, CR1_RXNEIE, CR1_TCIE, CR1_TXEIE = 1 << 2, 1 << 3, 1 << 4, 1 << 5, 1 << 6, 1 << 7
CR1_PEIE, CR1_PS, CR1_PCE, CR1_WAKE, CR1_M, CR1_UE, CR1_OVER8 = 1 << 8, 1 << 9, 1 << 10, 1 << 11, 1 << 12, 1 << 13, 1 << 15
CR2_LBDIE, CR2_CLKEN, CR2_LINEN = 1 << 6, 1 << 11, 1 << 14
CR3_EIE, CR3_IREN, CR3_HDSEL, CR3_SCEN = 1 << 0, 1 << 1, 1 << 3, 1 << 5
CR3_DMAR, CR3_DMAT, CR3_RTSE, CR3_CTSE, CR3_CTSIE = 1 << 6, 1 << 7, 1 << 8, 1 << 9, 1 << 10
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
                     "on_read": self._sr_read},
              "DR": {"on_read": self._dr_read, "on_write": self._dr_write},
              "BRR": {"on_write": self._ctl_write},
              "CR1": {"on_write": self._cr1_write},
              "CR2": {"on_write": self._ctl_write},
              "CR3": {"on_write": self._cr3_write}}
        tbl = name if name[:-1] in ("USART", "UART") else f"{'UART' if self.kind == 'uart' else 'USART'}{self.index}"
        r = build_regs((name, tbl, "USART1"), size, ov, params.get("regs_file"))
        self._rx_init()
        super().__init__(name, base, size, r, log=log, params=params)
        self.output = bytearray()
        self.rx_signal = f"{name}_RX"
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
            enr, bit = GATES[self.gate]
            rst = _RST_REG.get(enr)
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
        super().reset()
        self._dma_bits = 0
        self._dma_update()
        self._wire_reset()
        self._cr3 = 0
        self._tx_hold = self._tx_cur = None
        self._tx_left_ns = None
        self._brk_pending = self._brk_active = False
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

    def usartdiv(self) -> float:
        brr = self.get("BRR") & 0xFFFF
        if self.get("CR1") & CR1_OVER8:
            return (brr >> 4) + (brr & 7) / 8.0
        return (brr >> 4) + (brr & 0xF) / 16.0

    def baud(self) -> float:
        div = self.usartdiv()
        if div <= 0:
            return 0.0
        over8 = 1 if self.get("CR1") & CR1_OVER8 else 0
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
        if self._brk_active:
            self._brk_active = False
            self.set("CR1", self.get("CR1") & ~CR1_SBK)
        b, self._tx_cur = self._tx_cur, None
        if b is not None and self._cr3 & CR3_HDSEL and self._rx_enabled():
            self._rx_arrive(b, "loop")                 # single-wire half duplex: the receiver sees the line
        if self._brk_pending:
            self._start_break()
            return
        if self._tx_hold is not None:
            if self.cts_asserted() and not self._stopped:
                h, self._tx_hold = self._tx_hold, None
                self._start_tx(h)
            return                                     # TXE/TC stay 0 while the character waits for CTS
        self._sr_mod(SR_TXE | SR_TC)

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
        if self._tx_ev is not None or self._stopped or self._brk_pending or not self.cts_asserted():
            # TDR: waits for the shift register (character or break in progress), CTS or the clock (Stop mode)
            self._tx_hold = b
            self._sr_mod(clr_bits=SR_TXE | SR_TC)
            return old
        self._start_tx(b)
        return old

    def _start_tx(self, b: int) -> None:
        self.output.append(b & 0xFF)
        self.emit("uart", uart=self.name, dir="tx", byte=b & 0xFF)
        for cb in self.tx_listeners:
            cb(self, b & 0xFF)
        self._cancel("_tx_ev")
        t = self.char_ns()
        self._tx_cur = b & 0xFF
        if t > 0:
            self._sr_mod(clr_bits=SR_TXE | SR_TC)
            self._tx_end_ns = self.chip.now_ns() + t
            self._tx_ev = self.chip.sched.in_ns(t, self._tx_done)
        else:
            self._tx_done()
            if self._dma_tx:                    # no baud generator: TXE never dropped, the request stays
                dma_request(self.chip, self._dma_names[0], True)

    def _start_break(self) -> None:
        self._brk_pending = False
        self._brk_active = True
        self.emit("uart", uart=self.name, dir="tx", brk=1)
        b = self.baud()
        t = (self.data_bits() + 1 + self.stop_bits()) * 1e9 / b if b > 0 else 0.0
        if t > 0:
            self._tx_end_ns = self.chip.now_ns() + t
            self._tx_ev = self.chip.sched.in_ns(t, self._tx_done)
        else:
            self._tx_done()

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
        if ct > 0 and now - max(self._last_rx_ns, self._mute_since) >= 2 * ct - 1e-6:
            self.set("CR1", cr1 & ~CR1_RWU)                   # an idle frame preceded this character: wake up
            return True
        return False

    def receive_break(self) -> None:
        """A break (all-zero character with a zero stop bit) arrives now: FE + RXNE with 0x00; LBD with LINEN."""
        if self._rx_block_reason() is not None:
            return
        self.breaks_rx += 1
        self.emit("uart", uart=self.name, dir="rx", brk=1)
        if self.get("CR2") & CR2_LINEN:
            self._sr_mod(SR_LBD)
        self._rx_accept(0, framing=True)

    def _rx_accept(self, b: int, framing: bool = False) -> None:
        self._cancel("_idle_ev")
        cr1 = self.get("CR1")
        b &= 0x1FF if cr1 & CR1_M else 0xFF
        if cr1 & CR1_PCE:
            dmask = 0xFF if cr1 & CR1_M else 0x7F
            msb = dmask + 1
            b &= dmask
            par = bin(b).count("1") & 1
            if cr1 & CR1_PS:
                par ^= 1
            if par and not framing:
                b |= msb
        if (cr1 & (CR1_RWU | CR1_WAKE)) and not self._mute_filter(b, cr1):
            self._last_rx_ns = self.chip.now_ns()
            self.emit("uart", uart=self.name, dir="drop", byte=b & 0xFF, dropped=1, reason="mute")
            return
        self._last_rx_ns = self.chip.now_ns()
        if self.get("SR") & SR_RXNE:
            self._rx_drop(b, "overrun")
            self._sr_mod(SR_ORE, SR_IDLE)
        else:
            self._rx_byte = b
            self.set("DR", b)
            if not framing:
                self.emit("uart", uart=self.name, dir="rx", byte=b & 0xFF)
            self._sr_mod(SR_RXNE | (SR_FE if framing else 0), SR_IDLE)
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
        self._update_line()
        return None
