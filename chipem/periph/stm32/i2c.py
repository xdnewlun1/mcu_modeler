"""STM32F4 I2C (I2C1-3, RM0090 ch. 27 "Inter-integrated circuit (I2C) interface"), pin-level bus model.

The bus runs on the open-drain signals I2Cx_SDA / I2Cx_SCL of the pins in AF mode (chipem/periph/i2cwire.py):
START, address, data, ACK and STOP appear on the pins at the configured rate, board nets carry them to other chips
and I2C parts, and an address nobody acknowledges gives SR1.AF at the 9th SCL clock.

Clocking (§27.6.8 CCR, §27.6.9 TRISE): Tpclk1 = 1 / PCLK1 (the RCC model's APB1 clock).  Standard mode (F/S = 0):
Thigh = Tlow = CCR x Tpclk1.  Fast mode (F/S = 1): DUTY = 0 Thigh = CCR x Tpclk1, Tlow = 2 x CCR x Tpclk1; DUTY = 1
Thigh = 9 x CCR, Tlow = 16 x CCR.  START/repeated START setup and hold and STOP setup = Thigh; bus free = Tlow; SDA
changes Tlow / 4 after SCL falls.  TRISE is stored only (edges are instantaneous: no rise time is added to the
period).  CR2.FREQ is not used for timing; a FREQ that differs from the real PCLK1 (or is < 2 MHz) logs one `warn`.

Registers (SVD table): CR1, CR2, OAR1, OAR2, DR, SR1, SR2, CCR, TRISE.
* CR1: PE (0 releases the lines, aborts a transfer, clears the status), START (master: START when the bus is free;
  a START requested while another master holds the bus waits for its STOP, as on silicon; while master: repeated
  START after the byte in progress), STOP (master: STOP after the byte in progress; cleared when generated), ACK,
  POS (ACK applies to the next byte: the (N)ACK sent for a byte is the ACK value latched at the previous ACK slot, the
  address ACK slot included; that gives the HAL 2-byte reception), NOSTRETCH (slave: no clock stretching), ENGC
  (general call 0x00 answered), SWRST (holds the peripheral in reset: lines released, state and status cleared).
* SR1 (§27.6.6): SB (set after the START; cleared by reading SR1 then writing DR, which sends the address), ADDR
  (master: address acknowledged; slave: own address matched; cleared by reading SR1 then SR2), BTF (byte transfer
  finished with DR not serviced: the interface stretches SCL after the 9th clock until SR1 is read and DR accessed),
  ADD10 (10-bit header sent, master), STOPF (slave: STOP after the transfer; cleared by reading SR1 then writing
  CR1), RxNE, TxE (TxE set when DR is empty while transmitting; cleared by a DR write, START, STOP or PE = 0); error
  flags BERR, ARLO, AF, OVR (rc_w0: written 0 clears, 1 keeps).  SR2: MSL, BUSY (START .. STOP on the bus, any master),
  TRA, GENCALL, DUALF.
* Master transmitter: after ADDR is cleared TxE = 1; each DR write goes to the shift register when it is idle (TxE
  stays 1) or waits in DR (TxE = 0); a NACKed byte or address sets AF and the master holds SCL until STOP/START.
  Master receiver: clearing ADDR starts the first byte; bytes are received back to back while DR is free; a byte that
  completes while RxNE is still set stays in the shift register with BTF = 1 (SCL stretched after its ACK) until DR is
  read.  The ACK slot of each received byte drives CR1.ACK (POS above); DMA LAST: the byte after DMA EOT-1 is NACKed
  (EOT-1 = the RX stream's NDTR reaching 1, read when the ACK bit is driven).
* Slave (OAR1 7-bit ADD7 when ADDMODE = 0, OAR2 ADD2 when ENDUAL, 0x00 with ENGC; ACK must be 1 to answer):
  ADDR (TRA = master reads, DUALF, GENCALL) with SCL stretched until ADDR is cleared (unless NOSTRETCH).  Slave
  transmitter: TxE; a byte needed while DR is empty sets BTF and stretches SCL until DR is written; the master's NACK
  of the last byte sets AF.  Slave receiver: RxNE; a byte received while RxNE is set sets BTF (stretched until DR is
  read; with NOSTRETCH it is lost with OVR; a slave transmitter with NOSTRETCH and an empty DR sends the DR
  byte again with OVR, 27.3.4).  STOP after an acknowledged transfer sets STOPF.
* IRQs (§27.4): "I2Cx_EV" = ITEVTEN & (SB | ADDR | ADD10 | STOPF | BTF | ITBUFEN & (TxE | RxNE)); "I2Cx_ER" =
  ITERREN & (BERR | ARLO | AF | OVR | PECERR | TIMEOUT | SMBALERT); level lines.
* DMA (§27.3.8): CR2.DMAEN asserts "I2Cx_TX" while TxE (transmitting) and "I2Cx_RX" while RxNE (RM0090 Table 43).
Not modelled: 10-bit slave addressing (OAR1.ADDMODE = 1 never matches), SMBus (SMBUS, PEC, ALERT, ARP, timeouts),
bus errors (BERR), the fixed analog spike filter (the programmable FLTR filters are F42x/F43x only, §27.3.5), OTYPER push-pull on I2C pins (always open-drain), the rise time.
"""
from __future__ import annotations

import re
from collections import deque
from typing import Any

from ..i2cwire import WAIT_TX, I2CPort, I2CTiming
from ..registry import register
from ._util import GatedBlock, apb_hz, build_regs, dma_request, irq_number

# CR1
PE, SMBUS, ENGC, NOSTRETCH, START, STOP, ACK, POS, PEC, ALERT, SWRST = (
    1 << 0, 1 << 1, 1 << 6, 1 << 7, 1 << 8, 1 << 9, 1 << 10, 1 << 11, 1 << 12, 1 << 13, 1 << 15)
# CR2
ITERREN, ITEVTEN, ITBUFEN, DMAEN, LAST = 1 << 8, 1 << 9, 1 << 10, 1 << 11, 1 << 12
# SR1
SB, ADDR, BTF, ADD10, STOPF, RXNE, TXE = 1 << 0, 1 << 1, 1 << 2, 1 << 3, 1 << 4, 1 << 6, 1 << 7
BERR, ARLO, AF, OVR, PECERR, TIMEOUT, SMBALERT = 1 << 8, 1 << 9, 1 << 10, 1 << 11, 1 << 12, 1 << 14, 1 << 15
ERRORS = BERR | ARLO | AF | OVR | PECERR | TIMEOUT | SMBALERT
EVENTS = SB | ADDR | ADD10 | STOPF | BTF
# SR2
MSL, BUSY, TRA, GENCALL, DUALF = 1 << 0, 1 << 1, 1 << 2, 1 << 4, 1 << 7
# CCR
F_S, DUTY = 1 << 15, 1 << 14


@register("stm32.i2c")
class I2C(GatedBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        m = re.search(r"(\d+)$", name)
        self.index = int(params.get("index", m.group(1) if m else 1))
        self.gate = f"I2C{self.index}"
        ov = {"CR1": {"on_write": self._cr1_write},
              "CR2": {"on_write": self._cr2_write},
              "SR1": {"ro_mask": 0x20FF, "on_read": self._sr1_read, "on_write": self._sr1_write},
              "SR2": {"ro_mask": 0xFFFF, "on_read": self._sr2_read},
              "DR": {"on_read": self._dr_read, "on_write": self._dr_write}}
        r = build_regs((name, f"I2C{self.index}", "I2C1"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.sda_sig, self.scl_sig = f"I2C{self.index}_SDA", f"I2C{self.index}_SCL"
        self.port: I2CPort | None = None
        self._ev_irq = self._er_irq = None
        self._lines = (False, False)
        self._dma_lv = (False, False)
        self._warned_freq = False
        self.transfers: deque[tuple[str, int, bool]] = deque(maxlen=4096)   # ("w"/"r"/"sw"/"sr", byte|-1, acked)
        self._state_init()

    def _state_init(self) -> None:
        self._sr1_seen = 0                  # SR1 value of the last SR1 read (clear sequences)
        self._msl = False
        self._tra = False
        self._gencall = self._dualf = False
        self._rx = 0                    # DR read value
        self._shift_rx: int | None = None   # received byte waiting in the shift register (BTF)
        self._tx: int | None = None     # byte written to DR, not yet in the shift register
        self._start_pending = False
        self._stop_pending = False
        self._stopping = False
        self._wait_bus = False          # START requested while another master holds the bus
        self._addr_byte = 0
        self._ten = False               # 10-bit header sent (ADD10 phase)
        self._ack_latch = True
        self._rx_go = False             # master receiver: keep receiving
        self._slave_xfer = False        # addressed as slave in this transfer (STOPF on the STOP)
        self._slave_nack = False

    # ------------------------------------------------------------------ wiring
    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._ev_irq = irq_number(chip, f"I2C{self.index}_EV")
        self._er_irq = irq_number(chip, f"I2C{self.index}_ER")
        self.port = I2CPort(chip, self.sda_sig, self.scl_sig, self, self.timing, self._arb_lost)
        self.port.target.enabled = False

    def reset(self) -> None:
        if self.port is not None:
            self.port.reset()
            self.port.target.enabled = False
        if self.chip is not None:
            if self._lines[0] and self._ev_irq is not None:
                self.irq(self._ev_irq, False)
            if self._lines[1] and self._er_irq is not None:
                self.irq(self._er_irq, False)
            if self._dma_lv[0]:
                dma_request(self.chip, f"I2C{self.index}_TX", False)
            if self._dma_lv[1]:
                dma_request(self.chip, f"I2C{self.index}_RX", False)
        self._lines = (False, False)
        self._dma_lv = (False, False)
        self._state_init()
        super().reset()

    # ------------------------------------------------------------------ timing
    def module_hz(self) -> float:
        return apb_hz(self.chip, 1) if self.chip is not None else 16e6

    def timing(self) -> I2CTiming:
        ccr = self.get("CCR")
        n = max(ccr & 0xFFF, 1)
        t = 1e9 / max(self.module_hz(), 1.0)
        if ccr & F_S:
            hi, lo = (9 * n, 16 * n) if ccr & DUTY else (n, 2 * n)
        else:
            hi = lo = n
        return I2CTiming(low_ns=lo * t, high_ns=hi * t, buf_ns=lo * t)

    def baud(self) -> float:
        return self.timing().hz

    def _check_freq(self) -> None:
        if self._warned_freq:
            return
        f = self.get("CR2") & 0x3F
        real = self.module_hz() / 1e6
        if f < 2 or abs(f - real) >= 1.0:
            self._warned_freq = True
            self.emit("warn", block=self.name, msg=f"{self.name}: CR2.FREQ = {f} MHz but PCLK1 is {real:g} MHz "
                      "(RM0090: FREQ must match PCLK1, >= 2 MHz)")

    # ------------------------------------------------------------------ status, IRQs, DMA
    def _sr1(self, set_bits: int = 0, clear_bits: int = 0) -> None:
        self.set("SR1", (self.get("SR1") | set_bits) & ~clear_bits)
        self._update()

    def _update(self) -> None:
        if self.chip is None:
            return
        sr1, cr2 = self.get("SR1"), self.get("CR2")
        ev = bool(cr2 & ITEVTEN and (sr1 & EVENTS or (cr2 & ITBUFEN and sr1 & (TXE | RXNE))))
        er = bool(cr2 & ITERREN and sr1 & ERRORS)
        if (ev, er) != self._lines:
            if ev != self._lines[0] and self._ev_irq is not None:
                self.irq(self._ev_irq, ev)
            if er != self._lines[1] and self._er_irq is not None:
                self.irq(self._er_irq, er)
            self._lines = (ev, er)
        dma = bool(cr2 & DMAEN)
        lv = (dma and bool(sr1 & TXE) and self._tra, dma and bool(sr1 & RXNE))
        if lv != self._dma_lv:
            if lv[0] != self._dma_lv[0]:
                dma_request(self.chip, f"I2C{self.index}_TX", lv[0])
            if lv[1] != self._dma_lv[1]:
                dma_request(self.chip, f"I2C{self.index}_RX", lv[1])
            self._dma_lv = lv

    def _dma_rearm(self) -> None:
        """A DR access (possibly by the DMA, which acknowledges one item per request) left TxE/RxNE set: the request
        level is still high, so assert it again (as stm32.usart does)."""
        if self._dma_lv[0]:
            dma_request(self.chip, f"I2C{self.index}_TX", True)
        if self._dma_lv[1]:
            dma_request(self.chip, f"I2C{self.index}_RX", True)

    def _dma_last(self) -> bool:
        """DMA reception with CR2.LAST: is the byte being received the last one (DMA EOT-1 reached)?"""
        name = f"I2C{self.index}_RX"
        for c in getattr(self.chip, "_stm32_dmas", None) or self._dmas():
            for s in range(8):
                cr = c._crv(s)
                if cr & 1 and name in c.stream_request(s):
                    left = c._sreg(s, "NDTR") & 0xFFFF
                    if self.get("SR1") & RXNE:
                        left -= 1
                    return left <= 1
        return False

    def _dmas(self) -> list:
        from .dma import controllers
        return controllers(self.chip)

    # ------------------------------------------------------------------ register hooks
    def _sr1_read(self, blk, reg):
        self._sr1_seen = self.get("SR1")
        return None

    def _sr1_write(self, blk, reg, old, new):
        v = (old & 0x00FF) | (old & new & 0xDF00)        # error flags rc_w0
        self._store(reg, v)
        self._update()
        return v

    def _sr2(self) -> int:
        v = 0
        if self._msl:
            v |= MSL
        if self.port is not None and self.port.target.busy:
            v |= BUSY
        if self._tra:
            v |= TRA
        if self._gencall:
            v |= GENCALL
        if self._dualf:
            v |= DUALF
        return v

    def _sr2_read(self, blk, reg):
        v = self._sr2()
        if self._sr1_seen & ADDR and self.get("SR1") & ADDR:
            self._sr1_seen &= ~ADDR
            self._sr1(0, ADDR)
            self._addr_cleared()
        if not self._msl:
            self._gencall = self._dualf = False
        return v

    def _cr2_write(self, blk, reg, old, new):
        self._store(reg, new)
        self._update()
        return None

    def _cr1_write(self, blk, reg, old, new):
        port = self.port
        self._store(reg, new)
        if self._sr1_seen & STOPF and self.get("SR1") & STOPF:
            self._sr1_seen &= ~STOPF
            self._sr1(0, STOPF)
        if port is None:
            return None
        if new & SWRST or not new & PE:
            if old & PE and not old & SWRST or new & SWRST and not old & SWRST:
                port.reset()
                self._state_init()
                self.set("SR1", 0)
                self._store(reg, new & ~(START | STOP))
                self._update()
            port.target.enabled = False
            return None
        port.target.enabled = True
        if new & START and not old & START:
            self._req_start()
        if new & STOP and not old & STOP:
            self._req_stop()
        return None

    # ------------------------------------------------------------------ master control
    def _req_start(self) -> None:
        m = self.port.master
        self._check_freq()
        if self._msl:
            if m.busy:
                self._start_pending = True
            else:
                self._do_start()
        elif self.port.target.busy and not m.owns_bus:
            self._wait_bus = True                            # generated once the bus is free
        else:
            self._do_start()

    def _do_start(self) -> None:
        self._start_pending = False
        self._wait_bus = False
        self._rx_go = False
        self._msl = True
        self._sr1(0, TXE | BTF)
        self.port.master.start(self._start_done)

    def _start_done(self) -> None:
        self.set("CR1", self.get("CR1") & ~START)
        self._sr1(SB)
        if self._stop_pending:
            self._do_stop()

    def _req_stop(self) -> None:
        m = self.port.master
        if not self._msl:
            self.set("CR1", self.get("CR1") & ~STOP)          # slave: STOP releases the lines
            self.port.target.release()
            return
        if m.busy:
            self._stop_pending = True
        else:
            self._do_stop()

    def _do_stop(self) -> None:
        self._stop_pending = False
        self._start_pending = False
        self._rx_go = False
        self._stopping = True
        self._sr1(0, TXE | BTF)
        self.port.master.stop(self._stop_done)

    def _stop_done(self) -> None:
        self.set("CR1", self.get("CR1") & ~STOP)
        self._stopping = False
        self._msl = False
        self._tra = False
        self._update()

    def _after_byte(self) -> bool:
        """STOP / repeated START requested during the byte: generate it now. True if one was generated."""
        if self._stop_pending:
            self._do_stop()
            return True
        if self._start_pending:
            self._do_start()
            return True
        return False

    def _ack_fn(self) -> bool:
        cr1 = self.get("CR1")
        cur = bool(cr1 & ACK)
        if cr1 & POS:
            a, self._ack_latch = self._ack_latch, cur
        else:
            a = self._ack_latch = cur
        if a and self.get("CR2") & (DMAEN | LAST) == DMAEN | LAST and self._dma_last():
            a = False
        return a

    def _addr_done(self, acked: bool) -> None:
        byte = self._addr_byte
        self.transfers.append(("w", byte, acked))
        if not acked:
            self._sr1(AF)
            self._after_byte()
            return
        self._ack_latch = bool(self.get("CR1") & ACK)
        if (byte & 0xF9) == 0xF0 and not byte & 1 and not self._ten:
            self._ten = True                                 # 10-bit header: second address byte next
            self._sr1(ADD10)
            return
        self._ten = False
        self._tra = not byte & 1
        self._sr1(ADDR)
        self._after_byte()

    def _addr_cleared(self) -> None:
        if self._stopping or self._stop_pending and not self.port.master.busy:
            return
        if self._msl:
            if self._tra:
                self._sr1(TXE)
            else:
                self._rx_go = True
                self._next_rx()
        else:
            t = self.port.target
            if self._tra:
                self._sr1(TXE)
                if self._tx is not None:
                    b, self._tx = self._tx, None
                    self._slave_send(b)
            elif t.holding:
                t.release()

    def _next_rx(self) -> None:
        m = self.port.master
        if not m.busy and m.owns_bus and self._rx_go and not self.get("SR1") & BTF:
            m.read(self._ack_fn, self._rx_done)

    def _tx_done(self, acked: bool) -> None:
        self.transfers.append(("w", -1, acked))
        if not acked:
            self._sr1(AF)
            self._after_byte()
            return
        if self._after_byte():
            return
        if self._tx is not None:
            b, self._tx = self._tx, None
            self.port.master.write(b, self._tx_done)
            self._sr1(TXE)
        else:
            self._sr1(BTF | TXE)

    def _rx_done(self, byte: int, acked: bool) -> None:
        self.transfers.append(("r", byte, acked))
        if not self.get("SR1") & RXNE:
            self._rx = byte
            self._sr1(RXNE)
        else:
            self._shift_rx = byte
            self._sr1(BTF)
        if not acked:
            self._rx_go = False
        if self._after_byte():
            return
        self._next_rx()

    def _arb_lost(self) -> None:
        self._msl = False
        self._tra = False
        self._tx = None
        self._start_pending = self._stop_pending = False
        self._rx_go = False
        self.set("CR1", self.get("CR1") & ~(START | STOP))
        self._sr1(ARLO, SB | TXE | BTF)

    # ------------------------------------------------------------------ DR
    def _dr_write(self, blk, reg, old, new):
        self._dr_write_(new & 0xFF)
        self._dma_rearm()
        return None

    def _dr_write_(self, byte: int) -> None:
        port = self.port
        if port is None or not self.get("CR1") & PE:
            return
        sr1 = self.get("SR1")
        m = port.master
        if self._msl:
            if sr1 & SB or (sr1 & ADD10 and self._ten):
                self._sr1_seen &= ~(SB | ADD10)
                self._sr1(0, SB | ADD10 | TXE)
                self._addr_byte = byte
                if m.busy:
                    return
                m.write(byte, self._addr_done)
                return
            if not self._tra or sr1 & ADDR:
                return
            if sr1 & BTF:
                self._sr1_seen &= ~BTF
                self._sr1(0, BTF)
            if not m.busy and m.owns_bus:
                m.write(byte, self._tx_done)
                self._sr1(TXE)
            else:
                self._tx = byte
                self._sr1(0, TXE)
            return
        # slave transmitter
        if not self._tra:
            return
        t = port.target
        if sr1 & BTF or (t.holding and t.state == WAIT_TX and not sr1 & ADDR):
            self._sr1(0, BTF)
            self._slave_send(byte)
        else:
            self._tx = byte
            self._sr1(0, TXE)
        return

    def _slave_send(self, byte: int) -> None:
        self.transfers.append(("sr", byte, True))
        self._sr1(TXE)
        self.port.target.send(byte)

    def _dr_read(self, blk, reg):
        v = self._dr_read_()
        self._dma_rearm()
        return v

    def _dr_read_(self) -> int:
        v = self._rx
        sr1 = self.get("SR1")
        if not sr1 & RXNE:
            return v
        if self._shift_rx is not None:
            self._rx = self._shift_rx
            self._shift_rx = None
            self._sr1_seen &= ~BTF
            self._sr1(0, BTF)
            if self._msl:
                self._next_rx()
            else:
                self.port.target.release()
        else:
            self._sr1(0, RXNE)
        return v

    # ------------------------------------------------------------------ bus decoder / slave (I2CTarget handler)
    def i2c_bus(self, busy: bool) -> None:
        if not busy and self._wait_bus and self.get("CR1") & PE:
            self._do_start()

    def i2c_stop(self) -> None:
        if self._slave_xfer and not self._msl:
            self._slave_xfer = False
            if not self._slave_nack:
                self._sr1(STOPF)
            self._tra = False
        self._slave_nack = False

    def i2c_address(self, addr: int, read: bool) -> bool:
        cr1 = self.get("CR1")
        if not cr1 & PE or self._msl or self.port.master.owns_bus or not cr1 & ACK:
            return False
        oar1, oar2 = self.get("OAR1"), self.get("OAR2")
        gen = dual = False
        if not oar1 & (1 << 15) and addr == (oar1 >> 1) & 0x7F:
            pass
        elif oar2 & 1 and addr == (oar2 >> 1) & 0x7F:
            dual = True
        elif addr == 0 and cr1 & ENGC and not read:
            gen = True
        else:
            return False
        self._gencall, self._dualf = gen, dual
        self._tra = read
        self._slave_xfer = True
        self._slave_nack = False
        self._tx = None
        self._sr1(ADDR, TXE | BTF)
        return True

    def i2c_ack_done(self, read: bool) -> bool:
        if self.get("CR1") & NOSTRETCH:
            if self.get("SR1") & ADDR:
                self._sr1(0, ADDR)
                self._addr_cleared()
            return False
        sr1 = self.get("SR1")
        return bool(sr1 & (ADDR | BTF))

    def i2c_rx_byte(self, byte: int) -> bool:
        if not self.port.target.addressed or self._msl:
            return False
        self.transfers.append(("sw", byte, True))
        if not self.get("SR1") & RXNE:
            self._rx = byte
            self._sr1(RXNE)
        elif self.get("CR1") & NOSTRETCH:
            self._sr1(OVR)                                 # lost
        else:
            self._shift_rx = byte
            self._sr1(BTF)
        return bool(self.get("CR1") & ACK)

    def i2c_tx_byte(self):
        if self.get("SR1") & ADDR:
            return None
        if self._tx is not None:
            b, self._tx = self._tx, None
            self.transfers.append(("sr", b, True))
            self._sr1(TXE)
            return b
        if self.get("CR1") & NOSTRETCH:
            self._sr1(OVR)                                 # underrun: the byte in DR is sent again (RM0090 27.3.4)
            b = self.get("DR") & 0xFF
            self.transfers.append(("sr", b, True))
            return b
        self._sr1(BTF | TXE)
        return None

    def i2c_tx_ack(self, acked: bool) -> None:
        if not acked:
            self._slave_nack = True
            self._sr1(AF, TXE)
