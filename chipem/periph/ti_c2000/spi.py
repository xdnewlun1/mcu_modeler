"""C28x SPI-A (TMS320x2833x/2823x, 0x7040; SPRUI07 ch. 9 "Serial Peripheral Interface (SPI)"), bit-level on the pins.

Registers (chips/tms320f28232_regs.yaml "SPIA", 16-bit units, not EALLOW protected): SPICCR 0, SPICTL 1, SPISTS 2,
SPIBRR 4, SPIRXEMU 6, SPIRXBUF 7, SPITXBUF 8, SPIDAT 9, SPIFFTX A, SPIFFRX B, SPIFFCT C, SPIPRI F.  PCLKCR0.SPIAENCLK
gates the module (one `warn` on access with the clock off); PIE 6.1 SPIRXINTA, 6.2 SPITXINTA, looked up by name.

* Shift register (§9.3.2, §9.3.4, Example 9-1): SPIDAT is a 16-bit shift register.  Each bit shifts SPIDAT.15 out
  and the received bit into SPIDAT.0; after SPICHAR + 1 bits SPIDAT is copied to SPIRXBUF (and SPIRXEMU), so data
  written left-justified goes out MSB first and the received character is right-justified in SPIRXBUF with the
  remaining bits of the previous contents above it (the firmware masks them off).
* Transmit (§9.5.2.7): with SPISWRESET = 1, a write to SPIDAT (master) or to SPITXBUF while no character is being
  shifted falls through to SPIDAT and starts a master transfer (BUFFULL_FLAG stays 0); SPITXBUF written while
  shifting sets BUFFULL_FLAG and is moved to SPIDAT when the character ends, back to back.  A write while SPISWRESET
  = 0 is stored but not shifted.  Slave: SPIDAT / SPITXBUF are shifted on the master's clock.
* Clock (§9.3.5-9.3.6): master SPICLK = LSPCLK / (SPIBRR + 1), SPIBRR 0-2 -> LSPCLK / 4.  A bit is one SPICLK cycle:
  the inactive half (ceil((SPIBRR+1)/2) LSPCLK cycles: the low half when CLKPOLARITY = 0, the high half when 1, so an
  odd divider makes the inactive half one LSPCLK longer, Figure 9-6) then the active half.  CLK_PHASE = 0: data out
  on the leading edge (away from the idle level), latched on the trailing edge; CLK_PHASE = 1: the first bit is
  output as soon as the transfer starts (half a cycle before the first edge), later bits on trailing edges, input
  latched on leading edges.  Edges are scheduled in core cycles (`sched.at_cycles`) from LSPCLK = SYSCLKOUT /
  (2 x LOSPCP); one SPI character takes (SPICHAR + 1) x (SPIBRR + 1) LSPCLK cycles.
* Pins (master, MASTER_SLAVE = 1): SPICLKA output (idle level = CLKPOLARITY; driven 0 while SPISWRESET = 0,
  §9.5.2.1), SPISIMOA output while TALK = 1 (high impedance with TALK = 0), SPISOMIA input sampled at the latch
  edge (H = 1, L or floating = 0), SPISTEA output: driven low from the start of a character until the last one of a
  back-to-back sequence ends (§9.3.2 "driven low by the master before transmitting ... taken high after"), high
  otherwise.  Slave (MASTER_SLAVE = 0): SPICLKA/SPISIMOA/SPISTEA inputs; selected while SPISTEA is low (a slave
  whose SPISTEA is not muxed to a pin counts as always selected); SPISOMIA is driven while selected and TALK = 1,
  else high impedance; SPISTEA high stops the shift register (the bit count is kept until SPISWRESET clears it).
  Both GPIO16-19 and GPIO54-57 work (any pin muxed to the signal).  The GPIO mux model asks `signal_state()` for
  the live direction; outputs go through PinBoard.drive_signal, so a board net or another chip sees every edge.
* SPILBK = 1 (master only, §9.5.2.1): SIMO is looped to SOMI internally; SPICLK and SPISTE still toggle, SPISIMO /
  SPISOMI pins are not driven or sampled.
* Status / interrupts (§9.2.3, Table 9-2): INT_FLAG is set when a character completes (non-FIFO mode) and cleared
  by reading SPIRXBUF (not SPIRXEMU) or SPISWRESET = 0; a character completing while INT_FLAG is still set sets
  OVERRUN_FLAG (w1c) and overwrites SPIRXBUF.  Non-FIFO: SPIRXINTA (= SPIINT) = INT_FLAG & SPIINTENA |
  OVERRUN_FLAG & OVERRUNINTENA (level; the PIE latches the rising edge, so a set OVERRUN_FLAG requests once).
* FIFO mode (§9.3.7, SPIFFTX.SPIFFENA = 1 and SPIRST = 1): 16-word TX and RX FIFOs.  SPITXBUF writes go to the TX
  FIFO (dropped when full or while TXFIFO = 0, which flushes and holds it); a word moves to the shift register when
  it is idle, TXDLY SPICLK cycles after the previous word (SPIFFCT).  Received words go to the RX FIFO (RXFIFORESET =
  0 flushes and holds it); a 17th word sets RXFFOVF and the oldest word is lost.  SPIRXBUF reads pop the FIFO.
  TXFFST / RXFFST read the levels; TXFFINT is set while TXFFST <= TXFFIL and RXFFINT while RXFFST >= RXFFIL (set
  again after TXFFINTCLR / RXFFINTCLR while the condition holds); SPITXINTA = TXFFINT & TXFFIENA, SPIRXINTA =
  RXFFINT & RXFFIENA.  SPIRST = 0 resets the FIFO channels (both FIFOs flushed).
* SPISWRESET = 0 (§9.5.2.1): OVERRUN_FLAG, INT_FLAG and BUFFULL_FLAG cleared, any transfer stopped and its bit
  counter reset, SPISTE inactive, SPICLK driven 0 whatever CLKPOLARITY (returns to the idle level when SPISWRESET is
  set again).
* Responders (chipem/devices/spi_flash.py, board `devices: - {type: spi_flash, spi: SPIA, cs: GPIO19}` or `--spi-flash
  SPIA:cs=GPIO19`): `responder(tx, pcs, bits) -> rx` answers a master character instead of the SOMI pin, called
  once per byte of the character (bits > 8 are split MSB first); `frame_end(cont, eoq)` is called when a character
  ends (cont = True while SPISTE stays low for the next one, False when SPISTE goes high).  The device only sees the
  character when SPICLK and SPISIMO are muxed to pins (SOMI not muxed: received bits are 0).
Every character is recorded in `frames` as (tx, rx), right-justified to SPICHAR + 1 bits.
Not modelled: FREE/SOFT emulation suspend (stored), the one-SPICLK delay before SPICLK returns to its idle level
after SPISWRESET = 1, I/O buffer speed limits (one `warn` for a slave clock above LSPCLK / 4 is not issued either).
"""
from __future__ import annotations

from collections import deque
from typing import Any, Callable

from ..registry import register
from ._util import C2000Block, build_regs, gpio_resync, irq_line, lspclk_hz, listen_input, unlisten_input

# SPICCR
SPICHAR, SPILBK, CLKPOLARITY, SPISWRESET = 0xF, 1 << 4, 1 << 6, 1 << 7
# SPICTL
SPIINTENA, TALK, MASTER_SLAVE, CLK_PHASE, OVERRUNINTENA = 1, 2, 4, 8, 16
# SPISTS
BUFFULL, INT_FLAG, OVERRUN = 1 << 5, 1 << 6, 1 << 7
# SPIFFTX / SPIFFRX
SPIRST, SPIFFENA, TXFIFO = 1 << 15, 1 << 14, 1 << 13
RXFFOVF, RXFFOVFCLR, RXFIFORESET = 1 << 15, 1 << 14, 1 << 13
FFINT, FFINTCLR, FFIENA = 1 << 7, 1 << 6, 1 << 5
FIFO_DEPTH = 16


@register("ti_c2000.spi")
class SPI(C2000Block):
    cycle_anchors = ("_t0",)

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.letter = str(params.get("instance") or name[-1]).upper()
        ov = {
            "SPICCR": {"on_write": self._ccr_write, "ro_mask": 0xFF20},
            "SPICTL": {"on_write": self._ctl_write, "ro_mask": 0xFFE0},
            "SPISTS": {"w1c_mask": OVERRUN, "ro_mask": 0xFF7F, "on_write": lambda b, r, o, n: self._update()},
            "SPIBRR": {"ro_mask": 0xFF80},
            "SPIRXEMU": {"ro_mask": 0xFFFF, "on_read": self._rxemu_read},
            "SPIRXBUF": {"ro_mask": 0xFFFF, "on_read": self._rxbuf_read},
            "SPITXBUF": {"on_write": self._txbuf_write},
            "SPIDAT": {"on_write": self._dat_write, "on_read": lambda b, r: self.dat},
            "SPIFFTX": {"on_write": self._fftx_write, "ro_mask": 0x1F80},
            "SPIFFRX": {"on_write": self._ffrx_write, "ro_mask": 0x9F80},
            "SPIFFCT": {"ro_mask": 0xFF00},
            "SPIPRI": {"ro_mask": 0xFF8F},
        }
        regs = build_regs((name, f"SPI{self.letter}", "SPIA"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, regs, params=params, log=log)
        self.gate = params.get("gate", f"SPI{self.letter}ENCLK")
        self.sig = {k: f"SPI{k}{self.letter}" for k in ("CLK", "SIMO", "SOMI", "STE")}
        self.rx_irq = params.get("irq")
        self.tx_irq = params.get("tx_irq")
        self.responder: Callable[[int, int, int], int] | None = None
        self.frame_end: Callable[[bool, bool], None] | None = None
        self.frames: deque[tuple[int, int]] = deque(maxlen=100_000)
        self._listen: set[str] = set()
        self._warned: set[str] = set()
        self._lines = [False, False]
        self._init_state()

    def _init_state(self) -> None:
        self.dat = 0                    # SPIDAT shift register
        self.txbuf: int | None = None   # SPITXBUF waiting (BUFFULL_FLAG, non-FIFO)
        self.tx_fifo: deque[int] = deque()
        self.rx_fifo: deque[int] = deque()
        self._txffint = self._rxffint = False
        self._ev = None
        self.busy = False               # master: a character is being shifted (or waiting for TXDLY)
        self._bit = 0                   # bits shifted in the current character (master and slave)
        self._half = 0
        self._t0 = 0
        self._n = 1
        self._wire = False
        self._resp: int | None = None   # responder answer for the current master character
        self._tx_word = 0
        self._out: dict[str, str | None] = {"CLK": None, "SIMO": None, "SOMI": None, "STE": None}
        self._clk_lvl: str | None = None
        self._selected = False
        self._loaded = False            # slave FIFO mode: SPIDAT holds a word taken from the TX FIFO

    # ------------------------------------------------------------------ wiring
    def owned_signals(self):
        return tuple(self.sig.values())

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        if self.rx_irq is None:
            self.rx_irq = irq_line(chip, f"SPIRXINT{self.letter}", "6.1")
        if self.tx_irq is None:
            self.tx_irq = irq_line(chip, f"SPITXINT{self.letter}", "6.2")
        board = getattr(chip, "pins", None)
        if board is not None:
            for k in ("CLK", "SIMO", "STE"):
                for p in board.signal_pins(self.sig[k]):
                    if p.id not in self._listen:
                        self._listen.add(p.id)
                        listen_input(chip, p, self._pin_changed)

    def reset(self) -> None:
        if getattr(self, "_ev", None) is not None and self.chip is not None:
            self.chip.sched.cancel(self._ev)
        had = any(v is not None for v in getattr(self, "_out", {}).values())
        if self.chip is not None:
            for i, n in enumerate((self.rx_irq, self.tx_irq)):
                if self._lines[i]:
                    self.irq(n, False)
        self._lines = [False, False]
        self._init_state()
        self._warned.clear()
        super().reset()
        if had and self.chip is not None:
            for k in self.sig:
                self._release(k)
            self._resync()

    # ------------------------------------------------------------------ helpers
    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=msg)

    def _ccr(self) -> int:
        return self.get("SPICCR")

    def _ctl(self) -> int:
        return self.get("SPICTL")

    def running(self) -> bool:
        return bool(self._ccr() & SPISWRESET)

    def is_master(self) -> bool:
        return bool(self._ctl() & MASTER_SLAVE)

    def nbits(self) -> int:
        return (self._ccr() & SPICHAR) + 1

    def _fifo(self) -> bool:
        tx = self.get("SPIFFTX")
        return bool(tx & SPIFFENA and tx & SPIRST)

    def lsp_cycles(self) -> int:
        """Core (SYSCLKOUT) cycles per LSPCLK cycle."""
        if self.chip is None:
            return 4
        lsp = lspclk_hz(self.chip)
        return max(1, round(self.chip.clock.hz / lsp)) if lsp > 0 else 4

    def divider(self) -> int:
        brr = self.get("SPIBRR") & 0x7F
        return 4 if brr < 3 else brr + 1

    def baud(self) -> float:
        return lspclk_hz(self.chip) / self.divider() if self.chip is not None else 0.0

    def _pin_level(self, k: str) -> str | None:
        board = self.chip.pins
        sig = self.sig[k]
        for p in board.signal_pins(sig):
            if board.signal_matches(p.owner, sig):
                return p.level()
        return None

    def _muxed(self, k: str) -> bool:
        board = getattr(self.chip, "pins", None)
        if board is None:
            return False
        sig = self.sig[k]
        return any(board.signal_matches(p.owner, sig) for p in board.signal_pins(sig))

    # ------------------------------------------------------------------ pins
    def signal_state(self, sig: str) -> tuple[str, str | None] | None:
        for k, s in self.sig.items():
            if s == sig:
                lvl = self._out[k]
                return ("out", lvl) if lvl is not None else ("in", None)
        return None

    def _idle_clk(self) -> str:
        if not self.running():
            return "L"                                       # SPISWRESET = 0: SPICLK driven 0 (§9.5.2.1)
        return "H" if self._ccr() & CLKPOLARITY else "L"

    def _want_out(self) -> dict[str, str | None]:
        ctl = self._ctl()
        out: dict[str, str | None] = {"CLK": None, "SIMO": None, "SOMI": None, "STE": None}
        if ctl & MASTER_SLAVE:
            out["CLK"] = self._out["CLK"] if (self.busy and self._out["CLK"]) else self._idle_clk()
            out["STE"] = self._out["STE"] or "H"
            if ctl & TALK and not self._ccr() & SPILBK:
                out["SIMO"] = self._out["SIMO"] or "L"
        elif self._selected and ctl & TALK and self.running():
            out["SOMI"] = self._out["SOMI"] or ("H" if self.dat & 0x8000 else "L")
        return out

    def _apply_pins(self) -> None:
        if self.chip is None or self.chip.pins is None:
            return
        newly: list[str] = []
        changed = False
        for k, lvl in self._want_out().items():
            cur = self._out[k]
            if lvl is None:
                if cur is not None:
                    self._release(k)
                    changed = True
            elif cur is None:
                self._out[k] = lvl
                newly.append(k)
                changed = True
            elif lvl != cur:
                self._drive(k, lvl)
        if changed:
            self._resync()
        for k in newly:
            self.chip.pins.drive_signal(self.sig[k], self._out[k])

    def _resync(self) -> None:
        for s in self.sig.values():
            gpio_resync(self.chip, s)

    def _drive(self, k: str, lvl: str) -> None:
        if self._out[k] is None:
            return
        self._out[k] = lvl
        self.chip.pins.drive_signal(self.sig[k], lvl)

    def _release(self, k: str) -> None:
        self._out[k] = None
        if self.chip is not None and self.chip.pins is not None:
            self.chip.pins.release_signal(self.sig[k])

    # ------------------------------------------------------------------ status / IRQ
    def _sts(self, set_bits: int = 0, clr_bits: int = 0) -> None:
        self.set("SPISTS", (self.get("SPISTS") | set_bits) & ~clr_bits)
        self._update()

    def _update(self) -> None:
        if self.chip is None:
            return
        tx, rx = self.get("SPIFFTX"), self.get("SPIFFRX")
        fifo = self._fifo()
        if fifo and tx & TXFIFO and len(self.tx_fifo) <= (tx & 0x1F):
            self._txffint = True
        if fifo and rx & RXFIFORESET and len(self.rx_fifo) >= (rx & 0x1F):
            self._rxffint = True
        tx = (tx & ~(0x1F00 | FFINT)) | (len(self.tx_fifo) << 8) | (FFINT if self._txffint else 0)
        rx = (rx & ~(0x1F00 | FFINT)) | (len(self.rx_fifo) << 8) | (FFINT if self._rxffint else 0)
        self.set("SPIFFTX", tx)
        self.set("SPIFFRX", rx)
        if fifo:
            l_rx = bool(self._rxffint and rx & FFIENA)
            l_tx = bool(self._txffint and tx & FFIENA)
        else:
            st, ctl = self.get("SPISTS"), self._ctl()
            l_rx = bool(st & INT_FLAG and ctl & SPIINTENA) or bool(st & OVERRUN and ctl & OVERRUNINTENA)
            l_tx = False
        for i, (n, lvl) in enumerate(((self.rx_irq, l_rx), (self.tx_irq, l_tx))):
            if lvl != self._lines[i]:
                self._lines[i] = lvl
                self.irq(n, lvl)

    # ------------------------------------------------------------------ register hooks
    def _ccr_write(self, blk, reg, old, new):
        self._store(reg, new)
        if old & SPISWRESET and not new & SPISWRESET:
            self._enter_reset()
        self._select_changed()
        self._apply_pins()
        if new & SPISWRESET and not old & SPISWRESET:
            self._kick()
        return None

    def _enter_reset(self) -> None:
        self._stop(end_frame=True)
        self.txbuf = None
        self._bit = 0
        self.set("SPISTS", self.get("SPISTS") & ~(OVERRUN | INT_FLAG | BUFFULL))
        self._update()

    def _ctl_write(self, blk, reg, old, new):
        self._store(reg, new)
        if (old ^ new) & MASTER_SLAVE and self.busy:
            self._stop(end_frame=True)
        self._select_changed()
        self._apply_pins()
        self._update()
        if new & MASTER_SLAVE and not old & MASTER_SLAVE:
            self._kick()
        return None

    def _rxbuf_read(self, blk, reg):
        if self._fifo():
            v = self.rx_fifo.popleft() if self.rx_fifo else self.get("SPIRXBUF")
            self.set("SPIRXBUF", v)
            self.set("SPIRXEMU", v)
            self._update()
            return v
        if self.get("SPISTS") & INT_FLAG:
            self._sts(clr_bits=INT_FLAG)
        return self.get("SPIRXBUF")

    def _rxemu_read(self, blk, reg):
        if self._fifo():
            return self.rx_fifo[0] if self.rx_fifo else self.get("SPIRXBUF")
        return self.get("SPIRXBUF")

    def _dat_write(self, blk, reg, old, new):
        self.dat = new & 0xFFFF
        if self.is_master():
            self._kick(from_dat=True)
        elif self._selected and self._bit == 0 and self._ctl() & CLK_PHASE:
            self._slave_out()
        return None

    def _txbuf_write(self, blk, reg, old, new):
        v = new & 0xFFFF
        if self._fifo():
            tx = self.get("SPIFFTX")
            if tx & TXFIFO and len(self.tx_fifo) < FIFO_DEPTH:
                self.tx_fifo.append(v)
            elif tx & TXFIFO:
                self._warn_once("txff", f"{self.name}: SPITXBUF write with the TX FIFO full: word lost")
            self._update()
            if self.is_master():
                self._kick()
            else:
                self._slave_fill()
            return None
        shifting = self.busy if self.is_master() else self._bit > 0
        if shifting or not self.running():
            if shifting:
                self.txbuf = v
                self._sts(BUFFULL)
            else:
                self.dat = v                    # not shifted until a new character is written after SPISWRESET
            return None
        self.dat = v                            # falls through to SPIDAT (BUFFULL_FLAG not set)
        if self.is_master():
            self._kick(from_dat=True)
        elif self._selected and self._ctl() & CLK_PHASE:
            self._slave_out()
        return None

    def _fftx_write(self, blk, reg, old, new):
        if not new & TXFIFO or not new & SPIRST:
            self.tx_fifo.clear()
        if not new & SPIRST:
            self.rx_fifo.clear()
            self._rxffint = False
        if new & FFINTCLR:
            self._txffint = False
        self._store(reg, new & ~FFINTCLR)
        self._update()
        if self._fifo():
            if self.is_master():
                self._kick()
            else:
                self._slave_fill()
        return self.get("SPIFFTX")

    def _ffrx_write(self, blk, reg, old, new):
        if not new & RXFIFORESET:
            self.rx_fifo.clear()
        if new & FFINTCLR:
            self._rxffint = False
        keep_ovf = old & RXFFOVF and not new & RXFFOVFCLR
        self._store(reg, (new & ~(FFINTCLR | RXFFOVFCLR | RXFFOVF)) | (RXFFOVF if keep_ovf else 0))
        self._update()
        return self.get("SPIFFRX")

    # ------------------------------------------------------------------ data path
    def _next_tx(self) -> bool:
        """Load the next character into SPIDAT (TXBUF or TX FIFO).  True if one was loaded."""
        if self._fifo():
            if not self.tx_fifo:
                return False
            self.dat = self.tx_fifo.popleft()
            self._update()
            return True
        if self.txbuf is not None:
            self.dat, self.txbuf = self.txbuf, None
            self._sts(clr_bits=BUFFULL)
            return True
        return False

    def _complete(self, tx: int) -> None:
        """A character has been shifted: SPIDAT -> SPIRXBUF / RX FIFO, flags."""
        n = self.nbits()
        mask = (1 << n) - 1
        self.frames.append((tx & mask, self.dat & mask))
        rxv = self.dat
        if self._fifo():
            rx = self.get("SPIFFRX")
            if rx & RXFIFORESET:
                if len(self.rx_fifo) >= FIFO_DEPTH:
                    self.rx_fifo.popleft()
                    self.set("SPIFFRX", rx | RXFFOVF)
                    self._warn_once("rxffovf", f"{self.name}: RX FIFO overflow (RXFFOVF): oldest word lost")
                self.rx_fifo.append(rxv)
            self.set("SPIRXBUF", rxv)
            self.set("SPIRXEMU", rxv)
            self._update()
            return
        st = self.get("SPISTS")
        self.set("SPIRXBUF", rxv)
        self.set("SPIRXEMU", rxv)
        self._sts(INT_FLAG | (OVERRUN if st & INT_FLAG else 0))

    # ------------------------------------------------------------------ master engine
    def _kick(self, from_dat: bool = False) -> None:
        """Start a master character: SPIDAT was written (from_dat) or a queued word is due."""
        if self.chip is None or not self.running() or not self.is_master() or self.busy:
            return
        if not from_dat and not self._next_tx():
            return
        self._start_char(first=True)

    def _start_char(self, first: bool) -> None:
        self.busy = True
        self._bit = 0
        n = self.nbits()
        self._tx_word = self.dat
        div, lsp = self.divider(), self.lsp_cycles()
        self._n = div * lsp
        self._half = (div - div // 2) * lsp                 # inactive half (the longer one for odd dividers)
        self._t0 = self.chip.now_cycles()
        lbk = bool(self._ccr() & SPILBK)
        self._wire = self._muxed("CLK") and (lbk or self._muxed("SIMO"))
        self._resp = None
        self._apply_pins()
        self._drive("STE", "L")                              # before the device sees the character (chip select)
        if self.responder is not None and self._wire and not lbk:
            rx = 0
            tx = (self.dat >> (16 - n)) & ((1 << n) - 1)
            nb = (n + 7) // 8
            for k in range(nb):                              # bytes MSB first
                sh = max(0, n - 8 * (k + 1))
                bits = min(8, n - 8 * k)
                r = self.responder((tx >> sh) & ((1 << bits) - 1), 0, bits)
                rx = (rx << bits) | (r & ((1 << bits) - 1))
            self._resp = rx if self._muxed("SOMI") else 0
        if self._ctl() & CLK_PHASE:
            self._drive("SIMO", "H" if self.dat & 0x8000 else "L")
        if self._resp is None and not (self._muxed("CLK") or self._muxed("STE") or self._muxed("SIMO")):
            # no pin carries the transfer: timing only (received bits: SOMI floating -> 0)
            self._ev = self.chip.sched.at_cycles(self._t0 + n * self._n, self._master_fast, n)
            return
        self._ev = self.chip.sched.at_cycles(self._t0 + self._half, self._master_edge, 0)

    def _master_fast(self, n: int) -> None:
        self._ev = None
        lbk = bool(self._ccr() & SPILBK)
        for i in range(n):
            out = (self.dat >> 15) & 1
            b = out if lbk else self._sample_bit(i)
            self.dat = ((self.dat << 1) | b) & 0xFFFF
        self._master_done()

    def _sample_bit(self, i: int) -> int:
        if self._ccr() & SPILBK:
            return (self.dat >> 15) & 1
        if self._resp is not None:
            n = self.nbits()
            return (self._resp >> (n - 1 - i)) & 1
        return 1 if self._pin_level("SOMI") == "H" else 0

    def _master_edge(self, k: int) -> None:
        """Edge k of the current character: even = leading (away from idle), odd = trailing."""
        self._ev = None
        n = self.nbits()
        i = k // 2
        leading = k % 2 == 0
        pol_h = bool(self._ccr() & CLKPOLARITY)
        self._drive("CLK", ("L" if pol_h else "H") if leading else ("H" if pol_h else "L"))
        phase = bool(self._ctl() & CLK_PHASE)
        if leading != phase:            # phase 0: out on leading; phase 1: out on trailing (bit i+1)
            if not phase:
                self._drive("SIMO", "H" if self.dat & 0x8000 else "L")
            elif i + 1 < n:
                self._drive("SIMO", "H" if self.dat & 0x8000 else "L")
        else:                           # latch
            b = self._sample_bit(i)
            self.dat = ((self.dat << 1) | b) & 0xFFFF
        if k + 1 < 2 * n:
            t = self._t0 + (i + 1) * self._n + (0 if leading else self._half)
            self._ev = self.chip.sched.at_cycles(t, self._master_edge, k + 1)
        else:
            self._master_done()

    def _master_done(self) -> None:
        tx = (self._tx_word >> (16 - self.nbits()))
        self._complete(tx)
        self.busy = False
        self._bit = 0
        more = False
        if self.running() and self.is_master():
            if self._fifo():
                dly = self.get("SPIFFCT") & 0xFF
                if self.tx_fifo and dly:
                    self._end_frame(False)
                    self.busy = True                        # waiting TXDLY SPICLK cycles before the next word
                    self._ev = self.chip.sched.in_cycles(dly * self._n, self._after_delay)
                    return
            more = self._next_tx()
        self._end_frame(more)
        if more:
            self._start_char(first=False)

    def _after_delay(self) -> None:
        self._ev = None
        self.busy = False
        self._kick()

    def _end_frame(self, cont: bool) -> None:
        if self.frame_end is not None and self._wire:
            self.frame_end(cont, False)
        if not cont:
            self._drive("STE", "H")

    def _stop(self, end_frame: bool = False) -> None:
        if self._ev is not None and self.chip is not None:
            self.chip.sched.cancel(self._ev)
        self._ev = None
        was = self.busy
        self.busy = False
        self._bit = 0
        if was and end_frame and self.chip is not None:
            self._end_frame(False)

    # ------------------------------------------------------------------ slave engine
    def _compute_selected(self) -> bool:
        if self.is_master() or not self.running():
            return False
        lvl = self._pin_level("STE") if self.chip is not None and self.chip.pins is not None else None
        return lvl is None or lvl == "L"            # SPISTEA not muxed: always selected

    def _select_changed(self) -> None:
        sel = self._compute_selected()
        if sel == self._selected:
            return
        self._selected = sel
        if sel:
            self._clk_lvl = self._pin_level("CLK")
            self._apply_pins()
            if self._bit == 0 and self._ctl() & CLK_PHASE:
                self._slave_out()
        else:
            self._apply_pins()

    def _slave_out(self) -> None:
        if self._out["SOMI"] is not None:
            self._drive("SOMI", "H" if self.dat & 0x8000 else "L")

    def _slave_fill(self) -> None:
        """FIFO mode slave: an idle shift register takes the next TX FIFO word."""
        if self._bit == 0 and not self._loaded and self.tx_fifo:
            self._next_tx()
            self._loaded = True
            if self._selected and self._ctl() & CLK_PHASE:
                self._slave_out()

    def _pin_changed(self, pin: Any) -> None:
        if self.chip is None:
            return
        board = self.chip.pins
        owner = pin.owner
        if board.signal_matches(owner, self.sig["STE"]):
            if not self.is_master():
                self._select_changed()
            return
        if not board.signal_matches(owner, self.sig["CLK"]) or self.is_master() or not self._selected:
            return
        lvl = pin.level()
        prev = self._clk_lvl
        if lvl == prev:
            return
        self._clk_lvl = lvl
        if lvl not in ("H", "L") or prev not in ("H", "L"):
            return
        leading = (lvl == "H") != bool(self._ccr() & CLKPOLARITY)
        phase = bool(self._ctl() & CLK_PHASE)
        if leading != phase:
            if not phase or self._bit > 0:
                self._slave_out()
        else:
            b = 1 if self._pin_level("SIMO") == "H" else 0
            if self._bit == 0:
                self._tx_word = self.dat
            self.dat = ((self.dat << 1) | b) & 0xFFFF
            self._bit += 1
            if self._bit >= self.nbits():
                self._bit = 0
                self._loaded = False
                self._complete(self._tx_word >> (16 - self.nbits()))
                if self._next_tx():
                    self._loaded = True
                if phase:
                    self._slave_out()
