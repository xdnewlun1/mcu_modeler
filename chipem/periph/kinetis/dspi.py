"""Kinetis DSPI (SPI0-2, K22 RM ch. 47), master mode, frame-level model with instant transfers.

* MCR: MSTR (31), MDIS (14), DIS_TXF (13), DIS_RXF (12), CLR_TXF (11) / CLR_RXF (10) (flush, self-clearing),
  ROOE (24), HALT (0; reset HALT=1, MDIS=1).  The module runs (SR.TXRXS=1) while HALT=0, MDIS=0 and EOQF=0.
* PUSHR write: the command+data entry enters the TX FIFO; while running each entry is shifted out at once: the
  frame size is CTARn.FMSZ+1 bits (CTAS selects CTAR0/1), the received word comes from `responder(tx, pcs,
  bits)` if set, else `params.miso` (default all ones: nothing drives MISO, the line idles high).  The received
  word enters the RX FIFO (depth `params.fifo` / 4 for SPI0, 1 otherwise; overflow sets RFOF and, with
  ROOE=0, drops the word).  Each frame sets TCF and increments TCR.SPI_TCNT (PUSHR.CTCNT clears it first);
  PUSHR.EOQ sets EOQF, which stops the module until EOQF is cleared (w1c).
* POPR read pops the RX FIFO.  SR: TCF, EOQF, TFUF, TFFF, RFOF, RFDF are w1c; TFFF (TX FIFO not full), RFDF (RX
  FIFO not empty), TXCTR, RXCTR, TXNXTPTR/POPNXTPTR and TXRXS are live.  TXFRn/RXFRn mirror the FIFOs.
* IRQ "SPI<n>" = OR of (flag & RSER enable) for TCF/EOQF/TFUF/RFOF and TFFF/RFDF unless routed to DMA
  (RSER.TFFF_DIRS/RFDF_DIRS); DMA requests use the DMAMUX sources SPI0 RX/TX = 14/15, SPI1 = 16, SPI2 = 17.
* Every frame is recorded in `self.frames` (pcs, tx, rx); `self.output` collects the transmitted bytes.
  Devices (chipem.devices) plug in through `responder` and `frame_end(cont, eoq)`.
Slave mode, continuous SCK and timing (baud, delays) are not modelled.
"""
from __future__ import annotations

from collections import deque
from typing import Any, Callable

from ..registry import register
from ._util import GatedBlock, build_regs, irq_number
from .dma import dma_request

MCR_MSTR, MCR_ROOE, MCR_MDIS, MCR_DIS_TXF, MCR_DIS_RXF = 1 << 31, 1 << 24, 1 << 14, 1 << 13, 1 << 12
MCR_CLR_TXF, MCR_CLR_RXF, MCR_HALT = 1 << 11, 1 << 10, 1
SR_TCF, SR_TXRXS, SR_EOQF, SR_TFUF, SR_TFFF, SR_RFOF, SR_RFDF = (1 << 31, 1 << 30, 1 << 28, 1 << 27, 1 << 25,
                                                                 1 << 19, 1 << 17)
W1C = SR_TCF | SR_EOQF | SR_TFUF | SR_TFFF | SR_RFOF | SR_RFDF
RSER_TFFF_DIRS, RSER_RFDF_DIRS = 1 << 24, 1 << 16
PUSHR_CONT, PUSHR_EOQ, PUSHR_CTCNT = 1 << 31, 1 << 27, 1 << 26


@register("kinetis.dspi")
class DSPI(GatedBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        digits = "".join(ch for ch in name if ch.isdigit())
        self.index = int(params.get("index", digits or 0))
        self.gate = f"SPI{self.index}"
        ov = {"MCR": {"on_write": self._mcr_write},
              "SR": {"w1c_mask": W1C, "ro_mask": ~W1C & 0xFFFFFFFF, "on_read": self._sr_read,
                     "on_write": self._sr_write},
              "RSER": {"on_write": lambda b, r, o, n: self._update()},
              "PUSHR": {"on_write": self._pushr_write},
              "POPR": {"ro_mask": 0xFFFFFFFF, "on_read": self._popr_read}}
        for i in range(4):
            ov[f"TXFR{i}"] = {"ro_mask": 0xFFFFFFFF, "on_read": self._fifo_read(True, i)}
            ov[f"RXFR{i}"] = {"ro_mask": 0xFFFFFFFF, "on_read": self._fifo_read(False, i)}
        r = build_regs((name, f"SPI{self.index}", "SPI0"), size, ov, params.get("regs_file"),
                       skip=("CTAR_SLAVE", "PUSHR_SLAVE"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.depth = int(params.get("fifo", 4 if self.index == 0 else 1))
        self.miso = int(params.get("miso", 0xFFFF))
        self.responder: Callable[[int, int, int], int] | None = None
        self.frame_end: Callable[[bool, bool], None] | None = None   # cb(cont, eoq) after each frame
        self.txq: deque[int] = deque()
        self.rxq: deque[int] = deque()
        self.frames: deque[tuple[int, int, int]] = deque(maxlen=100_000)
        self.output = bytearray()
        self._last_rx = 0
        self._line = False
        self._irqn: int | None = None
        self._busy = False
        self.dma_rx = self.dma_tx = -1

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irqn = irq_number(chip, f"SPI{self.index}", self.name)
        rx, tx = {0: (14, 15), 1: (16, 16), 2: (17, 17)}.get(self.index, (-1, -1))
        self.dma_rx = int(self.params.get("dma_rx", rx))
        self.dma_tx = int(self.params.get("dma_tx", tx))
        for b in chip.peripherals.values():
            if getattr(b, "model", None) == "kinetis.dmamux":
                b.register_source(self.dma_rx, lambda: self._dma_rx_active())
                if self.dma_tx != self.dma_rx:
                    b.register_source(self.dma_tx, lambda: self._dma_tx_active())

    def reset(self) -> None:
        if getattr(self, "_line", False) and self.chip is not None and self._irqn is not None:
            self.irq(self._irqn, False)
        self._line = False
        self.txq.clear()
        self.rxq.clear()
        self._last_rx = 0
        super().reset()

    # ------------------------------------------------------------------ state
    def running(self) -> bool:
        mcr, sr = self.get("MCR"), self.get("SR")
        return not (mcr & (MCR_HALT | MCR_MDIS)) and not sr & SR_EOQF

    def _txdepth(self) -> int:
        return 1 if self.get("MCR") & MCR_DIS_TXF else self.depth

    def _rxdepth(self) -> int:
        return 1 if self.get("MCR") & MCR_DIS_RXF else self.depth

    def _status(self) -> int:
        v = self.get("SR") & (SR_TCF | SR_EOQF | SR_TFUF | SR_RFOF)
        if len(self.txq) < self._txdepth():
            v |= SR_TFFF
        if self.rxq:
            v |= SR_RFDF
        if self.running():
            v |= SR_TXRXS
        n_tx, n_rx = len(self.txq), len(self.rxq)
        return v | (min(n_tx, 15) << 12) | (min(n_rx, 15) << 4)

    # ------------------------------------------------------------------ transfers
    def _shift(self) -> None:
        """Transmit queued frames while the module runs."""
        if self._busy:
            return
        self._busy = True
        try:
            while self.txq and self.running():
                cmd = self.txq.popleft()
                ctas = (cmd >> 28) & 7
                ctar = self.get("CTAR1" if ctas == 1 else "CTAR0")
                bits = ((ctar >> 27) & 0xF) + 1
                mask = (1 << bits) - 1
                pcs = (cmd >> 16) & 0x3F
                tx = cmd & mask
                if cmd & PUSHR_CTCNT:
                    self.set("TCR", self.get("TCR") & 0xFFFF)
                rx = self.responder(tx, pcs, bits) if self.responder is not None else self.miso
                rx &= mask
                self.frames.append((pcs, tx, rx))
                if bits <= 8:
                    self.output.append(tx)
                else:
                    self.output += tx.to_bytes(2, "big")
                sr = self.get("SR") | SR_TCF
                if len(self.rxq) < self._rxdepth():
                    self.rxq.append(rx)
                else:
                    sr |= SR_RFOF
                    if self.get("MCR") & MCR_ROOE:   # overwrite the newest entry
                        self.rxq.pop()
                        self.rxq.append(rx)
                if cmd & PUSHR_EOQ:
                    sr |= SR_EOQF
                self.set("SR", sr)
                self.set("TCR", (self.get("TCR") + 0x10000) & 0xFFFFFFFF)
                if self.frame_end is not None:
                    self.frame_end(bool(cmd & PUSHR_CONT), bool(cmd & PUSHR_EOQ))
        finally:
            self._busy = False
        self._update()

    # ------------------------------------------------------------------ IRQ / DMA
    def _dma_rx_active(self) -> bool:
        return bool(self.rxq) and bool(self.get("RSER") & RSER_RFDF_DIRS) and bool(self.get("RSER") & SR_RFDF)

    def _dma_tx_active(self) -> bool:
        return (len(self.txq) < self._txdepth() and bool(self.get("RSER") & RSER_TFFF_DIRS)
                and bool(self.get("RSER") & SR_TFFF))

    def _update(self) -> None:
        if self.chip is None:
            return
        st, rser = self._status(), self.get("RSER")
        en = rser & (SR_TCF | SR_EOQF | SR_TFUF | SR_RFOF)
        if not rser & RSER_TFFF_DIRS:
            en |= rser & SR_TFFF
        if not rser & RSER_RFDF_DIRS:
            en |= rser & SR_RFDF
        line = bool(st & en)
        if line != self._line and self._irqn is not None:
            self._line = line
            self.irq(self._irqn, line)
        if not self._busy:
            if self._dma_rx_active():
                dma_request(self.chip, self.dma_rx)
            if self._dma_tx_active():
                dma_request(self.chip, self.dma_tx)

    # ------------------------------------------------------------------ register hooks
    def _mcr_write(self, blk, reg, old, new):
        if new & MCR_CLR_TXF:
            self.txq.clear()
        if new & MCR_CLR_RXF:
            self.rxq.clear()
        new &= ~(MCR_CLR_TXF | MCR_CLR_RXF)
        self._store(reg, new)
        self._shift()
        return new

    def _sr_read(self, blk, reg):
        return self._status()

    def _sr_write(self, blk, reg, old, new):
        self._store(reg, new & (SR_TCF | SR_EOQF | SR_TFUF | SR_RFOF))
        self._shift()                         # clearing EOQF restarts the module
        return None

    def _pushr_write(self, blk, reg, old, new):
        if len(self.txq) < self._txdepth():
            self.txq.append(new)
        self._shift()
        return None

    def _popr_read(self, blk, reg):
        if self.rxq:
            self._last_rx = self.rxq.popleft()
            self._update()
        return self._last_rx

    def _fifo_read(self, tx: bool, i: int):
        def rd(blk, reg):
            q = self.txq if tx else self.rxq
            return q[i] if i < len(q) else 0
        return rd
