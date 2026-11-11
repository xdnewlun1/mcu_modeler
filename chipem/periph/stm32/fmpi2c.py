"""STM32F446 FMPI2C1: the Fast-mode Plus I2C controller ("I2C v2" IP), RM0390 Rev 9 ch. 23, pin-level bus model.

Same bus engine as stm32.i2c (chipem/periph/i2cwire.py): START, address, data, ACK and STOP appear on the open-drain
signals FMPI2C1_SDA / FMPI2C1_SCL of the pins in AF mode, board nets carry them to the I2C parts and other chips.

Clock and timing (23.4.3, 23.4.9, 23.9.5 TIMINGR): the kernel clock I2CCLK comes from RCC_DCKCFGR2.FMPI2C1SEL
(stm32.rcc_f446 `fmpi2c_clk_hz()`: APB1 / SYSCLK / HSI).  tPRESC = (PRESC + 1) x tI2CCLK; tSCLL = (SCLL + 1) x tPRESC,
tSCLH = (SCLH + 1) x tPRESC, tSDADEL = SDADEL x tPRESC (SDA change after SCL falls), tSCLDEL = (SCLDEL + 1) x tPRESC
(SCL kept low after the SDA change, so the low phase is max(tSCLL, tSDADEL + tSCLDEL)).  The period includes the
synchronisation delays of 23.4.9: tSCL = tSYNC1 + tSYNC2 + tSCLL + tSCLH with tSYNC = (2 + DNF) x tI2CCLK per phase
(the RM gives "two to three I2CCLK periods" plus DNF x tI2CCLK plus the analog filter delay; the model takes 2 and no
analog filter delay).  tHD:STA and tSU:STO = tSCLH, tSU:STA and tBUF = tSCLL (23.9.5 notes).  Fast-mode Plus is simply
a faster TIMINGR; the SYSCFG_CFGR FMPI2C1_SCL/SDA (20 mA drive) bits are storage in SYSCFG.

Registers (23.9; FMPI2C1 is not in STM32F446.svd, the layout is RM0390 Table 143; reserved bits read 0):
* CR1: PE (0 = 23.4.6 software reset: lines released, CR2 START/STOP/NACK/PECBYTE and every ISR flag back to reset,
  TXE = 1; configuration kept), TXIE/RXIE/ADDRIE/NACKIE/STOPIE/TCIE/ERRIE, TXDMAEN/RXDMAEN, NOSTRETCH, ANFOFF, DNF[3:0]
  (the three can only change while PE = 0: a change with PE = 1 is ignored with one `warn`), SBC, GCEN, SMBHEN/SMBDEN
  (the SMBus host 0x08 / device default 0x61 addresses are acknowledged), ALERTEN, PECEN (stored, see below).
* CR2: SADD[9:0], RD_WRN, ADD10, HEAD10R, START, STOP, NACK, NBYTES[7:0], RELOAD, AUTOEND, PECBYTE.  START/STOP/NACK/
  PECBYTE are "rs": writing 0 has no effect, hardware clears them.
* OAR1 (OA1EN, OA1MODE, OA1[9:0]) and OAR2 (OA2EN, OA2MSK[2:0], OA2[7:1]); the address fields can only be written
  while the enable bit is 0 (23.9.3/23.9.4); with OA2MSK != 0 the reserved addresses 0b0000xxx / 0b1111xxx are not
  acknowledged.
* ISR (23.9.7): TXE (write 1 flushes TXDR), TXIS (write 1 only with NOSTRETCH = 1), RXNE, ADDR, NACKF, STOPF, TC, TCR,
  BERR, ARLO, OVR, PECERR, TIMEOUT, ALERT, BUSY, DIR, ADDCODE[6:0]; ICR (23.9.8) clears them (ADDRCF also clears
  CR2.START); RXDR / TXDR (TXDR is written only while TXE = 1).
* Controller (23.4.9): START sends START + address (7-bit SADD[7:1], or the 10-bit sequences of Figures 256/257 with
  HEAD10R), immediately or tBUF after the bus becomes free; START is cleared once the address is sent.  Transmitter: the
  TXDR -> shift register copy happens when the shift register needs the next byte (23.4.7 "Transmission"), TXIS asks for
  the following byte while one is still being shifted (NBYTES TXIS events per transfer), SCL is stretched while TXDR is
  empty.  Receiver: RXNE per byte, the last byte of the transfer (RELOAD = 0) is NACKed automatically.  After NBYTES
  bytes: RELOAD = 1 -> TCR, SCL stretched until NBYTES is written non-zero; AUTOEND = 1 -> STOP; else TC, SCL stretched
  until START (repeated START) or STOP.  A NACK (address or data) sets NACKF and sends a STOP automatically; STOPF is set
  by the STOP the controller generated.  Arbitration loss: ARLO, START cleared, back to target mode.
* Target (23.4.8): OA1 (7-bit), OA2 (masked), general call (GCEN), SMBus host/device addresses; ADDR + DIR + ADDCODE,
  SCL stretched until ADDRCF (NOSTRETCH = 0).  Transmitter: the byte in TXDR is sent (TXE = 1, TXIS requests the next
  one; TXDR empty: SCL stretched with TXIS set); the controller's NACK of the last byte sets NACKF (the byte loaded for
  the next read stays in TXDR: firmware flushes it with ISR.TXE = 1, as the HAL does).  Receiver: RXNE; a byte arriving
  with RXNE = 1 stretches SCL before its ACK until RXDR is read (NOSTRETCH = 1: OVR and a NACK); CR2.NACK sends a NACK for
  the current byte; SBC = 1 counts NBYTES and sets TCR (RELOAD = 1, SCL stretched until NBYTES is rewritten).  STOPF on
  the STOP after an addressed transfer.  With NOSTRETCH = 1 a byte requested while TXDR is empty is an underrun (OVR;
  the TXDR content is sent again).
* IRQs (23.6 Table 142): FMPI2C1_EV = TXIE.TXIS | RXIE.RXNE | ADDRIE.ADDR | NACKIE.NACKF | STOPIE.STOPF | TCIE.(TC|TCR);
  FMPI2C1_ER = ERRIE.(BERR | ARLO | OVR | PECERR | TIMEOUT | ALERT); level lines.
* DMA (23.7): "FMPI2C1_TX" while TXDMAEN and TXIS, "FMPI2C1_RX" while RXDMAEN and RXNE.

Approximations (documented in docs/LIMITATIONS.md): the controller receiver stretches SCL after the ACK of a byte that
finds RXDR full (silicon stretches between the 8th and 9th clock, Figure 245): the byte still counts as received; a
10-bit address the target NACKs is not resent in a loop (23.4.9) but ends with NACKF + STOP like a 7-bit one; misplaced
START/STOP (BERR) are not detected; edges are instantaneous (no rise time, the filters have nothing to filter).
Not modelled, one `warn` when enabled: SMBus PEC (PECEN / PECBYTE: PECR reads 0, PECERR never set), the SMBus timeouts
(TIMEOUTR TIMOUTEN / TEXTEN: TIMEOUT never set), SMBALERT (ALERTEN), 10-bit own address (OA1MODE = 1: never matches).
The F446 FMPI2C has no wake-up from Stop (CR1 bit 18 reserved): in Stop the block is frozen like the other APB1 blocks.
"""
from __future__ import annotations

from collections import deque
from typing import Any

from ..base import Reg, pure_read
from ..i2cwire import WAIT_TX, I2CPort, I2CTiming
from ..registry import register
from ._util import GatedBlock, HSI_HZ, apb_hz, dma_request, irq_number, rcc

# CR1
PE, TXIE, RXIE, ADDRIE, NACKIE, STOPIE, TCIE, ERRIE = (1 << i for i in range(8))
ANFOFF, TXDMAEN, RXDMAEN, SBC, NOSTRETCH, GCEN = 1 << 12, 1 << 14, 1 << 15, 1 << 16, 1 << 17, 1 << 19
SMBHEN, SMBDEN, ALERTEN, PECEN = 1 << 20, 1 << 21, 1 << 22, 1 << 23
CR1_MASK = 0x00FBDFFF
# CR2
RD_WRN, ADD10, HEAD10R, START, STOP, NACK = 1 << 10, 1 << 11, 1 << 12, 1 << 13, 1 << 14, 1 << 15
RELOAD, AUTOEND, PECBYTE = 1 << 24, 1 << 25, 1 << 26
RS_BITS = START | STOP | NACK | PECBYTE
# OAR1 / OAR2
OA1EN, OA1MODE, OA2EN = 1 << 15, 1 << 10, 1 << 15
# TIMEOUTR
TIMOUTEN, TEXTEN = 1 << 15, 1 << 31
# ISR
TXE, TXIS, RXNE, ADDR, NACKF, STOPF, TC, TCR = (1 << i for i in range(8))
BERR, ARLO, OVR, PECERR, TIMEOUT, ALERT, BUSY, DIR = 1 << 8, 1 << 9, 1 << 10, 1 << 11, 1 << 12, 1 << 13, 1 << 15, 1 << 16
ERRORS = BERR | ARLO | OVR | PECERR | TIMEOUT | ALERT
ICR_MASK = 0x3F38                      # ALERTCF TIMOUTCF PECCF OVRCF ARLOCF BERRCF STOPCF NACKCF ADDRCF



@register("stm32.fmpi2c")
class FMPI2C(GatedBlock):
    model = "stm32.fmpi2c"

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.gate = params.get("gate", "FMPI2C1")
        self.sig = params.get("signal", "FMPI2C1")
        zero = pure_read(lambda b, r: 0)                  # noqa: E731
        r = [Reg("CR1", 0x00, ro_mask=~CR1_MASK & 0xFFFFFFFF, on_write=self._cr1_write),
             Reg("CR2", 0x04, ro_mask=0xF8000000, on_write=self._cr2_write),
             Reg("OAR1", 0x08, ro_mask=0xFFFF7800, on_write=self._oar1_write),
             Reg("OAR2", 0x0C, ro_mask=0xFFFF7801, on_write=self._oar2_write),
             Reg("TIMINGR", 0x10, ro_mask=0x0F000000, on_write=self._timingr_write),
             Reg("TIMEOUTR", 0x14, ro_mask=0x70006000, on_write=self._timeoutr_write),
             Reg("ISR", 0x18, reset=TXE, ro_mask=0xFFFFFFFC, on_write=self._isr_write),
             Reg("ICR", 0x1C, on_read=zero, on_write=self._icr_write),
             Reg("PECR", 0x20, ro_mask=0xFFFFFFFF),
             Reg("RXDR", 0x24, ro_mask=0xFFFFFFFF, on_read=self._rxdr_read, on_peek=lambda b, r: self._rx),
             Reg("TXDR", 0x28, ro_mask=0xFFFFFF00, on_write=self._txdr_write)]
        super().__init__(name, base, size, r, log=log, params=params)
        self.ram_like = False                             # reserved offsets 0x2C.. read 0
        self.port: I2CPort | None = None
        self._ev_irq = self._er_irq = None
        self._lines = (False, False)
        self._dma_lv = (False, False)
        self._warned: set[str] = set()
        self._tcache: tuple = ()
        self.transfers: deque[tuple[str, int, bool]] = deque(maxlen=4096)   # ("w"/"r"/"sw"/"sr", byte|-1, acked)
        self._state_init()

    # ------------------------------------------------------------------ state
    def _state_init(self) -> None:
        self._mode: str | None = None       # "m" controller, "s" addressed target, None
        self._rx = 0                        # RXDR value
        self._shift_rx: int | None = None   # received byte waiting for RXDR (stretch)
        self._tx: int | None = None         # TXDR content when TXE = 0
        self._left = 0                      # bytes left in the current NBYTES chunk
        self._rd = False
        self._wait = ""                     # controller waiting for: "tx" (TXDR), "rx" (RXDR read), "tcr", "tc"
        self._stop_req = False
        self._wait_bus = False
        self._seq: list[tuple[str, int]] = []   # address phase steps ("w", byte) / ("rs", 0)
        self._s_hold = 0                    # target: reasons to stretch (bit 0 RXDR full, bit 1 TCR)
        self._s_count = False               # target byte control active in this transfer
        self._b = -1                        # byte being shifted out (controller)

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._ev_irq = irq_number(chip, f"{self.sig}_EV", f"{self.name}_EV")
        self._er_irq = irq_number(chip, f"{self.sig}_ER", f"{self.name}_ER")
        self.port = I2CPort(chip, f"{self.sig}_SDA", f"{self.sig}_SCL", self, self.timing, self._arb_lost)
        self.port.target.enabled = False
        self.port.master.lp_owner = self

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
                dma_request(self.chip, f"{self.sig}_TX", False)
            if self._dma_lv[1]:
                dma_request(self.chip, f"{self.sig}_RX", False)
        self._lines = (False, False)
        self._dma_lv = (False, False)
        self._state_init()
        super().reset()

    # ------------------------------------------------------------------ access (clock gate)
    def read(self, offset: int, size: int) -> int:
        if self._gate_loc is not None:
            self._gate_ok()
        return super().read(offset, size)

    def write(self, offset: int, size: int, value: int) -> None:
        if self._gate_loc is not None:
            self._gate_ok()
        super().write(offset, size, value)

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=f"{self.name}: {msg}")

    # ------------------------------------------------------------------ timing (23.4.9, 23.9.5)
    def kernel_hz(self) -> float:
        if self.chip is None:
            return HSI_HZ
        r = rcc(self.chip)
        f = getattr(r, "fmpi2c_clk_hz", None)
        hz = float(f()) if f is not None else 0.0
        return hz if hz > 0 else apb_hz(self.chip, 1)

    def timing(self) -> I2CTiming:
        tim, cr1 = self.get("TIMINGR"), self.get("CR1")
        hz = self.kernel_hz()
        key = (tim, cr1 & 0xF00, hz)
        if key == self._tcache[:3]:
            return self._tcache[3]
        tk = 1e9 / max(hz, 1.0)
        tp = ((tim >> 28) + 1) * tk
        scll, sclh = ((tim & 0xFF) + 1) * tp, (((tim >> 8) & 0xFF) + 1) * tp
        sdadel, scldel = ((tim >> 16) & 0xF) * tp, (((tim >> 20) & 0xF) + 1) * tp
        sync = (2 + ((cr1 >> 8) & 0xF)) * tk
        low = max(scll, sdadel + scldel) + sync
        t = I2CTiming(low_ns=low, high_ns=sclh + sync, hold_ns=max(sdadel, 1e-3), su_sta_ns=scll, hd_sta_ns=sclh,
                      su_sto_ns=sclh, buf_ns=scll)
        self._tcache = (*key, t)
        return t

    def baud(self) -> float:
        return self.timing().hz

    # ------------------------------------------------------------------ status, IRQs, DMA
    def _isr(self, set_bits: int = 0, clear_bits: int = 0) -> None:
        self.set("ISR", (self.get("ISR") | set_bits) & ~clear_bits)
        self._update()

    def _update(self) -> None:
        if self.chip is None:
            return
        isr, cr1 = self.get("ISR"), self.get("CR1")
        ev = bool((cr1 & TXIE and isr & TXIS) or (cr1 & RXIE and isr & RXNE) or (cr1 & ADDRIE and isr & ADDR)
                  or (cr1 & NACKIE and isr & NACKF) or (cr1 & STOPIE and isr & STOPF)
                  or (cr1 & TCIE and isr & (TC | TCR)))
        er = bool(cr1 & ERRIE and isr & ERRORS)
        if (ev, er) != self._lines:
            if ev != self._lines[0] and self._ev_irq is not None:
                self.irq(self._ev_irq, ev)
            if er != self._lines[1] and self._er_irq is not None:
                self.irq(self._er_irq, er)
            self._lines = (ev, er)
        lv = (bool(cr1 & TXDMAEN and isr & TXIS), bool(cr1 & RXDMAEN and isr & RXNE))
        if lv != self._dma_lv:
            if lv[0] != self._dma_lv[0]:
                dma_request(self.chip, f"{self.sig}_TX", lv[0])
            if lv[1] != self._dma_lv[1]:
                dma_request(self.chip, f"{self.sig}_RX", lv[1])
            self._dma_lv = lv

    def _dma_rearm(self) -> None:
        """A data register access (possibly by the DMA, which acknowledges one item per request) left TXIS/RXNE set:
        the request level is still high, so assert it again (as stm32.i2c / stm32.usart do)."""
        if self._dma_lv[0]:
            dma_request(self.chip, f"{self.sig}_TX", True)
        if self._dma_lv[1]:
            dma_request(self.chip, f"{self.sig}_RX", True)

    # ------------------------------------------------------------------ register hooks
    def _cr1_write(self, blk, reg, old, new):
        if old & PE and (old ^ new) & (NOSTRETCH | ANFOFF | 0xF00):
            self._warn_once("cr1pe", "CR1 NOSTRETCH/ANFOFF/DNF can only be programmed while PE = 0 "
                            "(RM0390 23.9.1): change ignored")
            new = (new & ~(NOSTRETCH | ANFOFF | 0xF00)) | (old & (NOSTRETCH | ANFOFF | 0xF00))
            self._store(reg, new)
        if new & (PECEN | SMBHEN | SMBDEN | ALERTEN) and new & PE:
            if new & (PECEN | ALERTEN):
                self._warn_once("smbus", "SMBus PEC / SMBALERT (CR1 PECEN / ALERTEN) are not modelled "
                                "(RM0390 23.4.11-23.4.15): PECR reads 0, PECERR/ALERT are never set")
        port = self.port
        if not new & PE:
            if old & PE:
                self._pe_reset()
            return new
        if port is not None:
            port.target.enabled = True
        self._update()
        return new

    def _pe_reset(self) -> None:
        """PE = 0 (RM0390 23.4.6): release the lines, reset the state machines, the CR2 control bits and the ISR
        flags; TXE = 1.  Configuration registers are kept."""
        if self.port is not None:
            self.port.reset()
            self.port.target.enabled = False
        self._state_init()
        self.set("CR2", self.get("CR2") & ~RS_BITS)
        self.set("ISR", TXE)
        self._update()

    def _cr2_write(self, blk, reg, old, new):
        new = (new & ~RS_BITS) | ((old | new) & RS_BITS)          # rs bits: writing 0 has no effect
        self._store(reg, new)
        if not self.get("CR1") & PE:
            new &= ~RS_BITS                                        # held in reset: cleared at once
            self._store(reg, new)
            return new
        if new & PECBYTE:
            self._warn_once("pecbyte", "CR2.PECBYTE: PEC transfer not modelled (RM0390 23.4.11)")
        if self.get("ISR") & TCR and (new >> 16) & 0xFF:
            self._reload(new)                                      # NBYTES written non-zero clears TCR (23.9.7)
        if new & START and not old & START:
            self._req_start()
        if new & STOP and not old & STOP:
            self._req_stop()
        return None

    def _oar1_write(self, blk, reg, old, new):
        if old & OA1EN:                                            # OA1 / OA1MODE writable only while OA1EN = 0
            new = (new & OA1EN) | (old & 0x7FF)
        if new & OA1EN and new & OA1MODE:
            self._warn_once("oa10", "10-bit own address (OAR1.OA1MODE = 1) is not modelled: never matches")
        return new

    def _oar2_write(self, blk, reg, old, new):
        if old & OA2EN:
            new = (new & OA2EN) | (old & 0x7FE)
        return new

    def _timingr_write(self, blk, reg, old, new):
        if self.get("CR1") & PE and new != old:
            self._warn_once("timingr", "TIMINGR written while PE = 1 (RM0390 23.9.5: configure it with PE = 0)")
        return None

    def _timeoutr_write(self, blk, reg, old, new):
        if old & TEXTEN:
            new = (new & ~0x0FFF0000) | (old & 0x0FFF0000)
        if old & TIMOUTEN:
            new = (new & ~0x1FFF) | (old & 0x1FFF)
        if new & (TIMOUTEN | TEXTEN):
            self._warn_once("timeout", "SMBus timeouts (TIMEOUTR TIMOUTEN / TEXTEN) are not modelled "
                            "(RM0390 23.4.11): TIMEOUT is never set")
        return new

    def _isr_write(self, blk, reg, old, new):
        v = old
        if new & TXE and not old & TXE:                            # flush TXDR
            self._tx = None
            v |= TXE
        if new & TXIS and self.get("CR1") & NOSTRETCH:
            v |= TXIS
        self._store(reg, v)
        self._update()
        return v

    def _icr_write(self, blk, reg, old, new):
        new &= ICR_MASK
        if new:
            clr = new & (ERRORS | STOPF | NACKF | ADDR)
            if new & ADDR:
                self.set("CR2", self.get("CR2") & ~START)
            had_addr = self.get("ISR") & ADDR
            self._isr(0, clr)
            if new & ADDR and had_addr:
                self._addr_cleared()
        return 0

    def _rxdr_read(self, blk, reg):
        v = self._rx
        if self.get("ISR") & RXNE:
            if self._shift_rx is not None:
                self._rx, self._shift_rx = self._shift_rx, None
                self._update()
                if self._mode == "m":
                    self._m_rx_continue()
                else:
                    self._s_unhold(1)
            else:
                self._isr(0, RXNE)
            self._dma_rearm()
        return v

    def _txdr_write(self, blk, reg, old, new):
        isr = self.get("ISR")
        if not isr & TXE:                                          # written only when TXE = 1 (23.9.11)
            return old
        self._tx = new & 0xFF
        self._isr(0, TXE | TXIS)
        if self._mode == "m" and self._wait == "tx":
            self._wait = ""
            self._m_tx_next()
        elif self._mode == "s" and self.port is not None:
            t = self.port.target
            if t.holding and t.state == WAIT_TX and not self.get("ISR") & ADDR:
                b = self._s_take()
                t.send(b)
        self._dma_rearm()
        return None

    # ------------------------------------------------------------------ controller
    def _req_start(self) -> None:
        isr = self.get("ISR")
        if isr & TCR:
            return                                                 # no effect while RELOAD stretches (23.9.2)
        port = self.port
        if port is None:
            return
        m = port.master
        if self._mode == "m":
            if self._wait == "tc":
                self._wait = ""
                self._isr(0, TC)
                self._do_start()
            return                                                 # mid transfer: RM - only after TC
        if port.target.busy and not m.owns_bus:
            self._wait_bus = True                                  # START once the bus is free
            return
        self._do_start()

    def _do_start(self) -> None:
        cr2 = self.get("CR2")
        self._wait_bus = False
        self._mode = "m"
        self._stop_req = False
        self._rd = bool(cr2 & RD_WRN)
        self._left = (cr2 >> 16) & 0xFF
        sadd = cr2 & 0x3FF
        if cr2 & ADD10:
            hdr = 0xF0 | ((sadd >> 7) & 6)
            if self._rd and cr2 & HEAD10R:
                self._seq = [("w", hdr | 1)]
            elif self._rd:
                self._seq = [("w", hdr), ("w", sadd & 0xFF), ("rs", 0), ("w", hdr | 1)]
            else:
                self._seq = [("w", hdr), ("w", sadd & 0xFF)]
        else:
            self._seq = [("w", (sadd & 0xFE) | int(self._rd))]
        self._isr(0, TC)
        self.port.master.start(self._addr_step)

    def _addr_step(self, acked: bool | None = None) -> None:
        if acked is False:
            self.set("CR2", self.get("CR2") & ~START)
            self._isr(NACKF)
            self._do_stop()
            return
        if not self._seq:
            self.set("CR2", self.get("CR2") & ~START)              # address sequence sent (23.9.2)
            self._data_phase()
            return
        op, b = self._seq.pop(0)
        m = self.port.master
        if op == "rs":
            m.start(self._addr_step)
        else:
            self.transfers.append(("w", b, True))
            m.write(b, self._addr_step)

    def _data_phase(self) -> None:
        if self._left == 0:
            self._chunk_end()
        elif self._rd:
            self._m_rx_next()
        else:
            self._m_tx_next()

    def _m_tx_next(self) -> None:
        if self._stop_req:
            self._do_stop()
            return
        if self._tx is None:
            self._wait = "tx"
            self._isr(TXIS)                                        # SCL stretched until TXDR is written
            return
        b, self._tx = self._tx, None
        self._left -= 1
        self._isr(TXE | (TXIS if self._left > 0 else 0))
        self._b = b
        self.port.master.write(b, self._m_tx_done)

    def _m_tx_done(self, acked: bool) -> None:
        self.transfers.append(("w", self._b, acked))
        if not acked:
            self._isr(NACKF)
            self._do_stop()                                        # automatic STOP after a NACK (23.4.9)
            return
        if self._stop_req:
            self._do_stop()
        elif self._left > 0:
            self._m_tx_next()
        else:
            self._chunk_end()

    def _m_ack(self) -> bool:
        if self._stop_req:
            return False
        return not (self._left == 1 and not self.get("CR2") & RELOAD)

    def _m_rx_next(self) -> None:
        if self._stop_req:
            self._do_stop()
            return
        self.port.master.read(self._m_ack, self._m_rx_done)

    def _m_rx_done(self, byte: int, acked: bool) -> None:
        self.transfers.append(("r", byte, acked))
        self._left -= 1
        if not self.get("ISR") & RXNE:
            self._rx = byte
            self._isr(RXNE)
            self._m_rx_continue()
        else:
            self._shift_rx = byte
            self._wait = "rx"

    def _m_rx_continue(self) -> None:
        if self._mode != "m" or self._shift_rx is not None:
            return
        self._wait = ""
        if self._stop_req:
            self._do_stop()
        elif self._left > 0:
            self._m_rx_next()
        else:
            self._chunk_end()

    def _chunk_end(self) -> None:
        cr2 = self.get("CR2")
        if cr2 & RELOAD:
            self._wait = "tcr"
            self._isr(TCR)
        elif cr2 & AUTOEND or self._stop_req:
            self._do_stop()
        else:
            self._wait = "tc"
            self._isr(TC)

    def _reload(self, cr2: int) -> None:
        """NBYTES written non-zero while TCR (23.9.7): next chunk."""
        self._isr(0, TCR)
        if self._mode == "m" and self._wait == "tcr":
            self._wait = ""
            self._left = (cr2 >> 16) & 0xFF
            self._data_phase()
        elif self._mode == "s" and self._s_count:
            self._left = (cr2 >> 16) & 0xFF
            self._s_unhold(2)

    def _req_stop(self) -> None:
        if self._mode != "m":
            self.set("CR2", self.get("CR2") & ~STOP)               # controller-only bit
            return
        if self._wait in ("tc", "tx", "tcr"):
            self._isr(0, TC | TCR)
            self._do_stop()
        else:
            self._stop_req = True                                  # after the current byte

    def _do_stop(self) -> None:
        self._stop_req = False
        self._wait = ""
        m = self.port.master
        if m.busy:
            self._stop_req = True
            return
        m.stop(self._stop_done)

    def _stop_done(self) -> None:
        self.set("CR2", self.get("CR2") & ~(STOP | START))
        self._mode = None
        self._isr(STOPF, TC | TCR)

    def _arb_lost(self) -> None:
        self._mode = None
        self._wait = ""
        self._stop_req = False
        self._seq = []
        self.set("CR2", self.get("CR2") & ~(START | STOP))
        self._isr(ARLO)

    # ------------------------------------------------------------------ target (I2CTarget handler)
    def i2c_bus(self, busy: bool) -> None:
        self._isr(BUSY if busy else 0, 0 if busy else BUSY)
        if not busy and self._wait_bus and self.get("CR1") & PE:
            self._do_start()

    def i2c_stop(self) -> None:
        if self._mode == "s":
            self._mode = None
            self._s_hold = 0
            self._s_count = False
            self.set("CR2", self.get("CR2") & ~(NACK | PECBYTE))
            self._isr(STOPF)

    lp_frozen: Any = False

    def _match(self, addr: int, read: bool) -> bool:
        cr1 = self.get("CR1")
        oar1, oar2 = self.get("OAR1"), self.get("OAR2")
        if oar1 & OA1EN and not oar1 & OA1MODE and addr == (oar1 >> 1) & 0x7F:
            return True
        if oar2 & OA2EN:
            msk = (oar2 >> 8) & 7
            keep = (0x7F << msk) & 0x7F
            if (addr & keep) == (((oar2 >> 1) & 0x7F) & keep):
                if msk == 0 or not ((addr >> 3) in (0, 0xF)):
                    return True
        if addr == 0 and cr1 & GCEN and not read:
            return True
        if addr == 0x08 and cr1 & SMBHEN:
            return True
        if addr == 0x61 and cr1 & SMBDEN:
            return True
        return False

    def i2c_address(self, addr: int, read: bool) -> bool:
        cr1 = self.get("CR1")
        if not cr1 & PE or self._mode == "m" or self.port.master.owns_bus or self.lp_frozen:
            return False
        if not self._match(addr, read):
            return False
        self._mode = "s"
        self._rd = read
        self._s_hold = 0
        self._s_count = bool(cr1 & SBC)
        if self._s_count:
            self._left = (self.get("CR2") >> 16) & 0xFF
        self.set("CR2", self.get("CR2") & ~(NACK | PECBYTE))
        isr = (self.get("ISR") & ~(DIR | (0x7F << 17))) | ADDR | (addr << 17) | (DIR if read else 0)
        self.set("ISR", isr)
        self._update()
        return True

    def i2c_ack_done(self, read: bool) -> bool:
        if self.get("CR1") & NOSTRETCH:
            return False
        if self.get("ISR") & ADDR:
            return True                                            # stretched until ADDRCF
        return bool(self._s_hold)

    def _addr_cleared(self) -> None:
        if self._mode != "s" or self.port is None:
            return
        t = self.port.target
        if self._rd:
            t.provide()
        elif not self._s_hold:
            t.release()

    def _s_unhold(self, bit: int) -> None:
        self._s_hold &= ~bit
        if not self._s_hold and self.port is not None and not self.get("ISR") & ADDR:
            self.port.target.release()

    def i2c_rx_byte(self, byte: int) -> bool:
        if self._mode != "s" or self.lp_frozen:
            return False
        cr1, cr2 = self.get("CR1"), self.get("CR2")
        self.transfers.append(("sw", byte, True))
        ack = not cr2 & NACK
        if cr2 & NACK:
            self.set("CR2", cr2 & ~NACK)
        if not self.get("ISR") & RXNE:
            self._rx = byte
            self._isr(RXNE)
            self._dma_rearm()
        elif cr1 & NOSTRETCH:
            self._isr(OVR)                                         # overrun: byte lost, NACKed (23.9.2 NACK note)
            return False
        else:
            self._shift_rx = byte
            self._s_hold |= 1
            self.port.target.hold()                                # stretch before the ACK pulse (23.4.7)
        if self._s_count:
            self._left -= 1
            if self._left <= 0 and cr2 & RELOAD:
                self._s_hold |= 2
                self._isr(TCR)
                self.port.target.hold()
        return ack

    def _s_take(self) -> int:
        b, self._tx = self._tx, None
        self.transfers.append(("sr", b, True))
        self._isr(TXE | TXIS)
        return b

    def i2c_tx_byte(self):
        if self.get("ISR") & ADDR and not self.get("CR1") & NOSTRETCH:
            return None
        if self._tx is not None:
            return self._s_take()
        if self.get("CR1") & NOSTRETCH:
            self._isr(OVR)                                         # underrun: the TXDR content is sent again
            b = self.get("TXDR") & 0xFF
            self.transfers.append(("sr", b, True))
            return b
        self._isr(TXIS)
        return None

    def i2c_tx_ack(self, acked: bool) -> None:
        if not acked:
            self._isr(NACKF)
