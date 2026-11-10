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
"""
from __future__ import annotations

from typing import Any

from ..base import MMIOBlock
from ..registry import register
from ..uartwire import UartRx
from ._util import GatedBlock, apb_hz, build_regs, dma_request, irq_number

SR_PE, SR_FE, SR_NF, SR_ORE, SR_IDLE, SR_RXNE, SR_TC, SR_TXE, SR_LBD, SR_CTS = (1 << b for b in range(10))
SR_RC_W0 = SR_RXNE | SR_TC | SR_LBD | SR_CTS
SR_ERR = SR_PE | SR_FE | SR_NF | SR_ORE | SR_IDLE
CR1_RE, CR1_TE, CR1_IDLEIE, CR1_RXNEIE, CR1_TCIE, CR1_TXEIE = 1 << 2, 1 << 3, 1 << 4, 1 << 5, 1 << 6, 1 << 7
CR1_PS, CR1_PCE, CR1_M, CR1_UE, CR1_OVER8 = 1 << 9, 1 << 10, 1 << 12, 1 << 13, 1 << 15
CR3_DMAR, CR3_DMAT = 1 << 6, 1 << 7
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

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irq = irq_number(chip, self.name, f"{'UART' if self.kind == 'uart' else 'USART'}{self.index}")
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
        self._set_line(bool((sr & SR_TXE and cr1 & CR1_TXEIE) or (sr & SR_TC and cr1 & CR1_TCIE)
                            or (sr & (SR_RXNE | SR_ORE) and cr1 & CR1_RXNEIE) or (sr & SR_IDLE and cr1 & CR1_IDLEIE)))

    def _sr_mod(self, set_bits: int = 0, clr_bits: int = 0) -> None:
        self.set("SR", (self.get("SR") | set_bits) & ~clr_bits)
        self._update_line()
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
        self._dma_bits = new & (CR3_DMAR | CR3_DMAT)
        self._dma_update()
        return None

    def _cancel(self, attr: str) -> None:
        ev = getattr(self, attr)
        if ev is not None:
            self.chip.sched.cancel(ev)
            setattr(self, attr, None)

    # ------------------------------------------------------------------ TX
    def _tx_done(self) -> None:
        self._tx_ev = None
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
        b = new & (0x1FF if cr1 & CR1_M and not cr1 & CR1_PCE else 0xFF)
        self.output.append(b & 0xFF)
        self.emit("uart", uart=self.name, dir="tx", byte=b & 0xFF)
        for cb in self.tx_listeners:
            cb(self, b & 0xFF)
        self._cancel("_tx_ev")
        t = self.char_ns()
        if t > 0:
            self._sr_mod(clr_bits=SR_TXE | SR_TC)
            self._tx_ev = self.chip.sched.in_ns(t, self._tx_done)
        else:
            self._sr_mod(SR_TXE | SR_TC)
            if self._dma_tx:                    # no baud generator: TXE never dropped, the request stays
                dma_request(self.chip, self._dma_names[0], True)
        return old

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
        return None

    def _rx_accept(self, b: int) -> None:
        self._cancel("_idle_ev")
        b &= 0x1FF if self.get("CR1") & CR1_M else 0xFF
        if self.get("SR") & SR_RXNE:
            self._rx_drop(b, "overrun")
            self._sr_mod(SR_ORE, SR_IDLE)
        else:
            self._rx_byte = b
            self.set("DR", b)
            self.emit("uart", uart=self.name, dir="rx", byte=b & 0xFF)
            self._sr_mod(SR_RXNE, SR_IDLE)
        t = self.char_ns()
        if t > 0:
            self._idle_ev = self.chip.sched.in_ns(t, self._idle)

    def _idle(self) -> None:
        self._idle_ev = None
        if self._rx_enabled():
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
        return None

    def _cr1_write(self, blk, reg, old, new):
        self._store(reg, new)
        if not (new & CR1_UE and new & CR1_RE):
            self._cancel("_idle_ev")
        self._update_line()
        return None
