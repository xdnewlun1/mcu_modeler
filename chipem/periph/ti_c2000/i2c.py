"""C28x I2C-A (TMS320x2833x/2823x, 0x7900): SPRUG03 "Inter-Integrated Circuit (I2C) Module Reference Guide"
(August 2008) and SPRUI07 (TMS320x2833x/2823x TRM, March 2020) ch. 11 (I2CEMDR); PCLKCR0.I2CAENCLK; PIE 8.1
I2CINT1A (basic interrupts) and 8.2 I2CINT2A (FIFO interrupts); pins GPIO32/SDAA and GPIO33/SCLA (GPBMUX1 = 1).

The bus is modelled at pin level (chipem/periph/i2cwire.py): SDAA/SCLA are open-drain signals; a board net (with
its pull-up) carries the bits to other chips and to I2C parts.  An address nobody acknowledges reads the released
SDA (1) in the ACK slot: I2CSTR.NACK.

Clocking (SPRUG03 §1.4, §5.5, §5.6.1): module clock = SYSCLKOUT / (IPSC + 1), IPSC latched when IRS goes 0 -> 1
(changes while IRS = 1 have no effect); one `warn` when the module clock is outside 7-12 MHz.  SCL low = Tmod x
(ICCL + d), high = Tmod x (ICCH + d), d = 7 (IPSC = 0), 6 (IPSC = 1), 5 (IPSC > 1).

I2CMDR (§5.1): IRS = 0 holds the module in reset: I2CSTR returns to its reset value (BB keeps its state, §5.3),
lines released, FIFOs flushed; STT/STP cannot be set.  Master (MST = 1): STT generates START (a repeated START when
we own the bus; START while BB is set by another master -> AL, MST/STP cleared, slave), then the address byte from
I2CSAR (XA = 0: 7 bits + R/W from TRX; XA = 1: 11110 A9 A8 W + A7..A0, and for a read a repeated START + 11110 A9
A8 R; FDF = 1: no address), then data bytes of BC bits (0 = 8; right-justified).  RM = 0: I2CCNT (0 = 65536) bytes,
then STOP if STP = 1, else ARDY and SCL held low (software sets STT for a repeated START or STP); RM = 1: data until
STP (ARDY after each transmitted byte).  Transmit: a byte is taken from I2CDXR (XRDY = 1 again) or the TX FIFO; with
none available XSMT = 0 and SCL is held low until I2CDXR is written.  Receive: the receiver ACKs each byte except
the last of a non-repeat transfer, any byte while STP = 1 in repeat mode, and the byte after NACKMOD = 1 (cleared
once sent; NACKSNT set); a byte arriving while I2CDRR (or the RX FIFO) is full stays in I2CRSR, RSFULL = 1 and SCL is
held low until I2CDRR is read.  A NACK received (address or data) sets NACK; the master then generates STOP if
STP = 1, else holds SCL low until STP/STT.  STT/STP clear when their condition has been generated; MST clears after
STOP.  DLB = 1 (digital loopback, master only): no pin activity; each byte written to I2CDXR appears in I2CDRR one
byte time (9 SCL periods) later, with the same count/ARDY/STOP sequencing.
Slave (MST = 0): 7-bit I2COAR match (or general call 0: AD0) -> AAS, SDIR = R/W; received bytes are ACKed (NACKMOD
-> NACK) and stored; a byte arriving with I2CDRR full -> RSFULL and SCL stretched until I2CDRR is read; slave
transmit takes I2CDXR (none: XSMT = 0, SCL stretched until written); the master's NACK sets NACK.
I2CSTR: AL, NACK, ARDY, RRDY, SCD, BB, NACKSNT, SDIR are w1c (writing 1 to BB clears it); XRDY, XSMT, AAS, AD0, RSFULL
read-only; BB = START seen .. STOP seen; SCD = STOP seen; AAS/AD0/SDIR clear on START/STOP.  I2CISRC: highest
priority enabled pending flag (1 AL, 2 NACK, 3 ARDY, 4 RRDY, 5 XRDY, 6 SCD, 7 AAS); reading it clears AL/NACK/SCD
when that is the code returned.  I2CINT1A = OR of I2CSTR flags & I2CIER (level).
FIFOs (§5.12-5.13): I2CFFTX.I2CFFEN enables FIFO mode; TXFFRST/RXFFRST = 0 hold a FIFO reset (flushed); TXFFST /
RXFFST count (16 deep); TXFFINT is set while TXFFST <= TXFFIL, RXFFINT while RXFFST >= RXFFIL (re-set after
TXFFINTCLR/RXFFINTCLR while the condition holds); I2CINT2A = TXFFINT & TXFFIENA | RXFFINT & RXFFIENA.
Not modelled: START byte mode (STB, stored), free data format as a slave, 10-bit slave addresses, I2CEMDR.BC timing
(stored), FREE (emulation), SCL/SDA noise filters.
"""
from __future__ import annotations

from collections import deque
from typing import Any

from ..i2cwire import I2CPort, I2CTiming
from ..registry import register
from ._util import C2000Block, build_regs, fmt_hz, irq_line, sysclk_hz

# I2CMDR
BC_MASK, FDF, STB, IRS, DLB, RM, XA, TRX, MST, STP, STT, FREE, NACKMOD = (
    7, 1 << 3, 1 << 4, 1 << 5, 1 << 6, 1 << 7, 1 << 8, 1 << 9, 1 << 10, 1 << 11, 1 << 13, 1 << 14, 1 << 15)
# I2CSTR
AL, NACK, ARDY, RRDY, XRDY, SCD, AD0, AAS, XSMT, RSFULL, BB, NACKSNT, SDIR = (
    1, 2, 4, 8, 16, 32, 1 << 8, 1 << 9, 1 << 10, 1 << 11, 1 << 12, 1 << 13, 1 << 14)
STR_W1C = SDIR | NACKSNT | BB | SCD | RRDY | ARDY | NACK | AL
STR_RESET = XSMT | XRDY
# I2CFFTX / I2CFFRX
I2CFFEN, TXFFRST, RXFFRST = 1 << 14, 1 << 13, 1 << 13
FFINT, FFINTCLR, FFIENA = 1 << 7, 1 << 6, 1 << 5
FIFO_DEPTH = 16
# I2CISRC codes in priority order (flag, code)
_PRIO = ((AL, 1), (NACK, 2), (ARDY, 3), (RRDY, 4), (XRDY, 5), (SCD, 6), (AAS, 7))


@register("ti_c2000.i2c")
class I2C(C2000Block):
    gate = "I2CAENCLK"

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.letter = str(params.get("instance") or name[-1]).upper()
        ov = {
            "I2CMDR": {"on_write": self._mdr_write, "ro_mask": 1 << 12},
            "I2CSTR": {"w1c_mask": STR_W1C, "ro_mask": 0xFFFF & ~STR_W1C, "on_write": self._str_write},
            "I2CIER": {"ro_mask": 0xFF80, "on_write": lambda b, r, o, n: self._update()},
            "I2CDXR": {"on_write": self._dxr_write, "ro_mask": 0xFF00},
            "I2CDRR": {"on_read": self._drr_read, "ro_mask": 0xFFFF},
            "I2CISRC": {"on_read": self._isrc_read, "ro_mask": 0xF0FF},
            "I2CFFTX": {"on_write": self._fftx_write, "ro_mask": 0x9F80},
            "I2CFFRX": {"on_write": self._ffrx_write, "ro_mask": 0xDF80},
            "I2CPSC": {"ro_mask": 0xFF00},
            "I2COAR": {"ro_mask": 0xFC00},
            "I2CSAR": {"ro_mask": 0xFC00},
        }
        regs = build_regs((name, f"I2C{self.letter}", "I2CA"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, regs, params=params, log=log)
        self.sda_sig, self.scl_sig = f"SDA{self.letter}", f"SCL{self.letter}"
        self.port: I2CPort | None = None
        self.int1 = params.get("irq")
        self.int2 = params.get("fifo_irq")
        self._l1 = self._l2 = False
        self._ipsc = 0
        self._warned_clk = False
        self.tx_fifo: deque[int] = deque()
        self.rx_fifo: deque[int] = deque()
        self._dxr_full = False
        self._rsr: int | None = None        # byte waiting in I2CRSR (RSFULL)
        self._phase: str | None = None      # master: None | "start" | "addr" | "data" | "hold"
        self._items: list = []
        self._count = 0
        self._rx = False
        self._stt_pending = False
        self._nack_seen = False
        self._dlb_ev = None
        self._txffint = self._rxffint = False
        self.transfers: list[tuple[str, int, bool]] = []

    # ------------------------------------------------------------------ wiring
    def attach(self, chip: Any) -> None:
        super().attach(chip)
        if self.int1 is None:
            self.int1 = irq_line(chip, f"I2CINT1{self.letter}", "8.1")
        if self.int2 is None:
            self.int2 = irq_line(chip, f"I2CINT2{self.letter}", "8.2")
        self.port = I2CPort(chip, self.sda_sig, self.scl_sig, self, self.timing, self._arb_lost)
        self.port.target.enabled = False

    def reset(self) -> None:
        self._cancel_dlb()
        if self.port is not None:
            self.port.reset()
            self.port.target.enabled = False
            self.port.target.busy = False
        self._set_lines(False, False)
        super().reset()
        self._soft_reset()
        self._ipsc = 0

    def _soft_reset(self) -> None:
        self.tx_fifo.clear()
        self.rx_fifo.clear()
        self._dxr_full = False
        self._rsr = None
        self._phase = None
        self._items = []
        self._stt_pending = False
        self._txffint = self._rxffint = False

    def _cancel_dlb(self) -> None:
        if self._dlb_ev is not None and self.chip is not None:
            self.chip.sched.cancel(self._dlb_ev)
        self._dlb_ev = None

    # ------------------------------------------------------------------ timing
    def module_hz(self) -> float:
        return sysclk_hz(self.chip) / (self._ipsc + 1) if self.chip is not None else 0.0

    def timing(self) -> I2CTiming:
        d = 7 if self._ipsc == 0 else (6 if self._ipsc == 1 else 5)
        tmod = 1e9 / max(self.module_hz(), 1.0)
        return I2CTiming(low_ns=tmod * ((self.get("I2CCLKL") & 0xFFFF) + d),
                         high_ns=tmod * ((self.get("I2CCLKH") & 0xFFFF) + d))

    def baud(self) -> float:
        return self.timing().hz

    def _nbits(self) -> int:
        bc = self.get("I2CMDR") & BC_MASK
        return bc or 8

    # ------------------------------------------------------------------ status / IRQ
    def _str(self, set_bits: int = 0, clear_bits: int = 0) -> None:
        self.set("I2CSTR", (self.get("I2CSTR") | set_bits) & ~clear_bits)
        self._update()

    def _fifo_on(self) -> bool:
        return bool(self.get("I2CFFTX") & I2CFFEN)

    def _tx_fifo_on(self) -> bool:
        return self._fifo_on() and bool(self.get("I2CFFTX") & TXFFRST)

    def _rx_fifo_on(self) -> bool:
        return self._fifo_on() and bool(self.get("I2CFFRX") & RXFFRST)

    def _update(self) -> None:
        if self.chip is None:
            return
        tx, rx = self.get("I2CFFTX"), self.get("I2CFFRX")
        if self._tx_fifo_on() and len(self.tx_fifo) <= (tx & 0x1F):
            self._txffint = True
        if self._rx_fifo_on() and len(self.rx_fifo) >= (rx & 0x1F):
            self._rxffint = True
        tx = (tx & ~(0x1F00 | FFINT)) | (min(len(self.tx_fifo), 16) << 8) | (FFINT if self._txffint else 0)
        rx = (rx & ~(0x1F00 | FFINT)) | (min(len(self.rx_fifo), 16) << 8) | (FFINT if self._rxffint else 0)
        self.set("I2CFFTX", tx)
        self.set("I2CFFRX", rx)
        st = self.get("I2CSTR")
        l1 = bool(st & self.get("I2CIER") & 0x7F & ~0) and bool(self._pending_code())
        l2 = bool(self._txffint and tx & FFIENA) or bool(self._rxffint and rx & FFIENA)
        self._set_lines(l1, l2)

    def _pending_code(self) -> int:
        st, ier = self.get("I2CSTR"), self.get("I2CIER")
        en = {AL: 1, NACK: 2, ARDY: 4, RRDY: 8, XRDY: 16, SCD: 32, AAS: 64}
        for flag, code in _PRIO:
            if st & flag and ier & en[flag]:
                return code
        return 0

    def _set_lines(self, l1: bool, l2: bool) -> None:
        if self.chip is None:
            self._l1, self._l2 = l1, l2
            return
        if l1 != self._l1:
            self._l1 = l1
            self.irq(self.int1, l1)
        if l2 != self._l2:
            self._l2 = l2
            self.irq(self.int2, l2)

    # ------------------------------------------------------------------ data path
    def _tx_take(self) -> int | None:
        if self._tx_fifo_on():
            if not self.tx_fifo:
                return None
            v = self.tx_fifo.popleft()
        else:
            if not self._dxr_full:
                return None
            self._dxr_full = False
            v = self.get("I2CDXR") & 0xFF
            self._str(XRDY)
        self._update()
        return v

    def _rx_put(self, byte: int) -> bool:
        """Deliver a received byte; False if the receiver is full (byte kept in I2CRSR, RSFULL)."""
        if self._rx_fifo_on():
            if len(self.rx_fifo) >= FIFO_DEPTH:
                self._rsr = byte
                self._str(RSFULL)
                return False
            self.rx_fifo.append(byte)
            self._update()
            return True
        if self.get("I2CSTR") & RRDY:
            self._rsr = byte
            self._str(RSFULL)
            return False
        self.set("I2CDRR", byte)
        self._str(RRDY)
        return True

    # ------------------------------------------------------------------ register hooks
    def _irs(self) -> bool:
        return bool(self.get("I2CMDR") & IRS)

    def _mdr_write(self, blk, reg, old, new):
        if not new & IRS:
            new &= ~(STT | STP)
            self._store(reg, new)
            if old & IRS:
                self._enter_reset()
            return new
        if not old & IRS:                                   # out of reset: latch the prescaler
            self._ipsc = self.get("I2CPSC") & 0xFF
            self.port.target.enabled = True
            self.port.target.release()
            hz = self.module_hz()
            if not self._warned_clk and not 7e6 <= hz <= 12e6:
                self._warned_clk = True
                self.emit("warn", block=self.name, msg=f"{self.name}: module clock {fmt_hz(hz)} outside 7-12 MHz "
                          "(SPRUG03 §1.4: I2C timing specifications not met)")
        self._store(reg, new)
        m = self.port.master
        if new & STT and not (old & STT and self._phase is not None):
            if new & MST:
                if self._phase is None and self.get("I2CSTR") & BB and not m.owns_bus and not new & DLB:
                    new &= ~(MST | STP | STT)
                    self._store(reg, new)
                    self._str(AL)                           # START attempted while the bus is busy
                    return new
                if self._phase is None or self._phase == "hold":
                    self._begin()
                else:
                    self._stt_pending = True
            else:
                new &= ~STT
                self._store(reg, new)
        if new & STP and self._phase in ("hold", "data") and not m.busy and self._dlb_ev is None:
            self._next()
        if (old ^ new) & NACKMOD:
            pass
        return None

    def _enter_reset(self) -> None:
        self._cancel_dlb()
        bb = self.get("I2CSTR") & BB
        self.port.reset()
        self.port.target.enabled = False
        self._soft_reset()
        self.set("I2CSTR", STR_RESET | bb)
        self._update()

    def _str_write(self, blk, reg, old, new):
        self._update()
        return None

    def _dxr_write(self, blk, reg, old, new):
        if not self._irs():
            return None
        byte = new & 0xFF
        if self._tx_fifo_on():
            if len(self.tx_fifo) < FIFO_DEPTH:
                self.tx_fifo.append(byte)
            self._txffint = len(self.tx_fifo) <= (self.get("I2CFFTX") & 0x1F) and self._txffint
        else:
            self._dxr_full = True
            self._str(0, XRDY)
        self._str(XSMT)
        t = self.port.target
        if t.addressed and t.read and t.state == 7:          # slave transmit waiting (WAIT_TX)
            b = self._tx_take()
            if b is not None:
                self.transfers.append(("sr", b, True))
                t.send(b)
        elif self._phase == "data" and not self.port.master.busy and self._dlb_ev is None:
            self._next()
        self._update()
        return None

    def _drr_read(self, blk, reg):
        if self._rx_fifo_on():
            v = self.rx_fifo.popleft() if self.rx_fifo else 0
        else:
            v = self.get("I2CDRR") & 0xFF
            self.set("I2CSTR", self.get("I2CSTR") & ~RRDY)
        if self._rsr is not None:                          # I2CRSR -> I2CDRR, release the stretched clock
            b, self._rsr = self._rsr, None
            self.set("I2CSTR", self.get("I2CSTR") & ~RSFULL)
            self._rx_put(b)
            t = self.port.target
            if t.holding:
                t.release()
            elif self._phase == "data" and not self.port.master.busy:
                self._next()
        self._update()
        return v

    def _isrc_read(self, blk, reg):
        code = self._pending_code()
        if code in (1, 2, 6):
            self.set("I2CSTR", self.get("I2CSTR") & ~{1: AL, 2: NACK, 6: SCD}[code])
            self._update()
        self.set("I2CISRC", (self.get("I2CISRC") & 0x0F00) | code)
        return (self.get("I2CISRC") & 0x0F00) | code

    def _fftx_write(self, blk, reg, old, new):
        if not new & TXFFRST:
            self.tx_fifo.clear()
        if new & FFINTCLR:
            self._txffint = False
        self._store(reg, new & ~FFINTCLR)
        self._update()
        return self.get("I2CFFTX")

    def _ffrx_write(self, blk, reg, old, new):
        if not new & RXFFRST:
            self.rx_fifo.clear()
        if new & FFINTCLR:
            self._rxffint = False
        self._store(reg, new & ~FFINTCLR)
        self._update()
        return self.get("I2CFFRX")

    # ------------------------------------------------------------------ master sequencing
    def _begin(self) -> None:
        """STT with MST: START (repeated if we own the bus) + address phase."""
        mdr = self.get("I2CMDR")
        self._stt_pending = False
        self._str(0, ARDY | NACK)
        self._count = (self.get("I2CCNT") & 0xFFFF) or 65536
        self._rx = not mdr & TRX
        sar = self.get("I2CSAR") & 0x3FF
        if mdr & DLB:
            sar = self.get("I2COAR") & 0x3FF
        if mdr & FDF:
            items: list = []
        elif mdr & XA:
            hdr = 0xF0 | ((sar >> 8) & 3) << 1
            items = [hdr, sar & 0xFF] + (["S", hdr | 1] if self._rx else [])
        else:
            items = [((sar & 0x7F) << 1) | (1 if self._rx else 0)]
        self._items = items
        self._nack_seen = False
        if mdr & DLB:
            self._phase = "data"
            self.set("I2CMDR", mdr & ~STT)
            self._str(BB)
            self._next()
            return
        self._phase = "start"
        self.port.master.start(self._start_done)

    def _start_done(self) -> None:
        self.set("I2CMDR", self.get("I2CMDR") & ~STT)
        self._str(BB)
        self._phase = "addr"
        self._next()

    def _next(self) -> None:
        """Advance the master at a byte boundary (or when software supplied what it waited for)."""
        m = self.port.master
        mdr = self.get("I2CMDR")
        if m.busy or self._dlb_ev is not None or self._phase is None:
            return
        if self._stt_pending and self._phase in ("hold", "data"):
            self._begin()
            return
        if self._phase == "addr":
            if self._items:
                it = self._items.pop(0)
                if it == "S":
                    m.start(lambda: self._next())
                else:
                    m.write(it, self._addr_done)
                return
            self._phase = "data"
        if self._phase == "hold":
            if mdr & STP:
                self._stop()
            return
        # data phase
        rm = bool(mdr & RM)
        if mdr & STP and (rm or self._count == 0):
            self._stop()
            return
        if not rm and self._count == 0:
            self._phase = "hold"
            self._str(ARDY)
            return
        if self._rx:
            if self._rsr is not None:
                return                                      # RSFULL: SCL held low until I2CDRR is read
            if mdr & DLB:
                return
            m.read(self._ack_fn, self._rx_done, nbits=self._nbits())
        else:
            b = self._tx_take()
            if b is None:
                self._str(0, XSMT)                          # underflow: SCL held low until I2CDXR is written
                return
            if mdr & DLB:
                self._dlb_ev = self.chip.sched.in_ns(9 * self.timing().period_ns, self._dlb_done, b)
                return
            m.write(b, lambda ack, _b=b: self._tx_done(_b, ack), nbits=self._nbits())

    def _ack_fn(self) -> bool:
        mdr = self.get("I2CMDR")
        nack = False
        if mdr & NACKMOD:
            nack = True
            self.set("I2CMDR", mdr & ~NACKMOD)
        elif mdr & RM:
            nack = bool(mdr & STP)
        else:
            nack = self._count == 1
        if nack:
            self._str(NACKSNT)
        return not nack

    def _addr_done(self, ack: bool) -> None:
        self.transfers.append(("a", -1, ack))
        if not ack:
            self._nack(True)
            return
        self._next()

    def _nack(self, master: bool) -> None:
        self._str(NACK)
        self._phase = "hold"
        if self.get("I2CMDR") & STP:
            self._stop()

    def _tx_done(self, byte: int, ack: bool) -> None:
        self.transfers.append(("w", byte, ack))
        mdr = self.get("I2CMDR")
        if not mdr & RM:
            self._count -= 1
        else:
            self._str(ARDY)
        if not ack:
            self._nack(True)
            return
        self._str(XSMT if (self._dxr_full or self.tx_fifo) else 0)
        self._next()

    def _rx_done(self, byte: int, acked: bool) -> None:
        self.transfers.append(("r", byte, acked))
        if not self.get("I2CMDR") & RM:
            self._count -= 1
        self._rx_put(byte & ((1 << self._nbits()) - 1))
        self._next()

    def _dlb_done(self, byte: int) -> None:
        self._dlb_ev = None
        self.transfers.append(("w", byte, True))
        if not self.get("I2CMDR") & RM:
            self._count -= 1
        else:
            self._str(ARDY)
        self._rx_put(byte & ((1 << self._nbits()) - 1))
        self._next()

    def _stop(self) -> None:
        self._phase = "stop"
        if self.get("I2CMDR") & DLB:
            self._stop_done()
            return
        self.port.master.stop(self._stop_done)

    def _stop_done(self) -> None:
        self._phase = None
        self.set("I2CMDR", self.get("I2CMDR") & ~(STP | MST))
        self._str(SCD, BB | AAS | AD0 | SDIR)
        if self._stt_pending:
            self._stt_pending = False

    def _arb_lost(self) -> None:
        self._phase = None
        self._stt_pending = False
        self.set("I2CMDR", self.get("I2CMDR") & ~(MST | STP | STT))
        self._str(AL)

    # ------------------------------------------------------------------ bus decoder / slave (I2CTarget handler)
    def i2c_bus(self, busy: bool) -> None:
        if not self._irs():
            return                                          # BB keeps its state while in reset (§5.3)
        self._str(BB if busy else 0, 0 if busy else BB)

    def i2c_start(self, repeated: bool) -> None:
        if self._irs():
            self._str(0, AAS | AD0 | SDIR)

    def i2c_stop(self) -> None:
        if self._irs():
            self._str(SCD, AAS | AD0 | SDIR)

    def i2c_address(self, addr: int, read: bool) -> bool:
        mdr = self.get("I2CMDR")
        if not mdr & IRS or mdr & MST or self.port.master.owns_bus or mdr & (XA | FDF):
            return False
        oar = self.get("I2COAR") & 0x7F
        if addr == oar:
            self._str(AAS | (SDIR if read else 0), AD0)
        elif addr == 0 and not read:
            self._str(AAS | AD0)
        else:
            return False
        return True

    def i2c_rx_byte(self, byte: int) -> bool:
        if not self.port.target.addressed:
            return False
        mdr = self.get("I2CMDR")
        ack = True
        if mdr & NACKMOD:
            ack = False
            self.set("I2CMDR", mdr & ~NACKMOD)
            self._str(NACKSNT)
        self.transfers.append(("sw", byte, ack))
        self._slave_full = not self._rx_put(byte)
        return ack

    def i2c_ack_done(self, read: bool) -> bool:
        if read:
            return False
        full = getattr(self, "_slave_full", False)
        self._slave_full = False
        return full                                         # RSFULL: stretch until I2CDRR is read

    def i2c_tx_byte(self):
        b = self._tx_take()
        if b is None:
            self._str(0, XSMT)                              # stretch until I2CDXR is written
            return None
        self.transfers.append(("sr", b, True))
        return b

    def i2c_tx_ack(self, acked: bool) -> None:
        if not acked:
            self._str(NACK)
