"""Kinetis eDMA (DMA, K22 RM ch. 22) and DMA request multiplexer (DMAMUX, ch. 21).

DMAMUX (`kinetis.dmamux`): CHCFGn (n = 0..15, one byte each): ENBL (7), TRIG (6, PIT periodic triggering: not
modelled, logged once; the channel then behaves as untriggered), SOURCE (5..0).  Request sources are numbered
as in the K22 RM DMAMUX table (`SOURCES`); 54..63 are "always enabled".  Peripherals that can request DMA call
`dma_request(chip, source)` when their request condition becomes true (e.g. UART RDRF with C5.RDMAS, ADC COCO
with SC2.DMAEN) and register `dmamux.register_source(source, active_fn)` so a channel enabled later (SERQ,
CHCFG.ENBL) picks up a request that is already pending.

DMA (`kinetis.dma`): 16 channels, TCDs at 0x1000 + 32*ch (SADDR, SOFF, ATTR, NBYTES, SLAST, DADDR, DOFF, CITER,
DLAST_SGA, CSR, BITER), control registers CR, ES, ERQ, EEI, the byte-wide set/clear registers CEEI/SEEI/CERQ/
SERQ/CDNE/SSRT/CERR/CINT (bit 6 = "all channels", bit 7 = no-op), INT and ERR (w1c), HRS (live request status).
A service request (peripheral request with ERQ set, an always-enabled source with ERQ set, TCD.CSR.START or
SSRT) executes ONE minor loop synchronously: NBYTES bytes are read in ATTR.SSIZE units from SADDR (+SOFF per
read, SMOD modulo) and written in ATTR.DSIZE units to DADDR (+DOFF, DMOD), then the minor-loop offset (CR.EMLM)
is applied and CITER decremented.  Minor-loop channel linking (CITER.ELINK) and, at major-loop completion,
SLAST/DLAST adjustment or scatter/gather (CSR.ESG), DONE, INT (CSR.INTMAJOR; INTHALF at the half-way point),
ERQ clear (CSR.DREQ) and major-loop linking (CSR.MAJORELINK) follow the RM.  The channel IRQ "DMA<ch>" is the
level of INT[ch]; bus errors set ERR/ES and raise "DMA_Error" when EEI[ch].  Channel priorities/arbitration and
bandwidth control are not modelled: transfers complete instantly (zero bus time).
"""
from __future__ import annotations

import struct
from typing import Any, Callable

from ..base import Reg
from ..registry import register
from ._util import GatedBlock, find_model, irq_number

NCH = 16
TCD_BASE = 0x1000
ALWAYS_ON = range(54, 64)
# K22 DMA request sources (subset), K22P100M120SF5V2RM Table 3-25 "DMA request sources - MUX 0" (section 3.3.9.1).
# 11 and 46 are Reserved there (no UART5 / DAC1 on the 100-pin K22); 20-39 are FTM channels
# (the FTM model does not raise DMA requests: CnSC.DMA is stored only).
SOURCES = {"UART0_RX": 2, "UART0_TX": 3, "UART1_RX": 4, "UART1_TX": 5, "UART2_RX": 6, "UART2_TX": 7,
           "UART3_RX": 8, "UART3_TX": 9, "UART4": 10, "I2S0_RX": 12, "I2S0_TX": 13,
           "SPI0_RX": 14, "SPI0_TX": 15, "SPI1": 16, "SPI2": 17, "I2C0": 18, "I2C1": 19, "I2C2": 19,
           "ADC0": 40, "ADC1": 41, "CMP0": 42, "CMP1": 43, "CMP2": 44, "DAC0": 45, "CMT": 47,
           "PDB": 48, "PORTA": 49, "PORTB": 50, "PORTC": 51, "PORTD": 52, "PORTE": 53}

# CHCFG
ENBL, TRIG = 0x80, 0x40
# CR
CR_HALT, CR_EMLM, CR_CX, CR_ECX = 1 << 5, 1 << 7, 1 << 17, 1 << 16
# TCD CSR
CSR_START, CSR_INTMAJOR, CSR_INTHALF, CSR_DREQ, CSR_ESG, CSR_MAJORELINK, CSR_ACTIVE, CSR_DONE = (
    1, 2, 4, 8, 0x10, 0x20, 0x40, 0x80)
# ES
ES_DBE, ES_SBE, ES_NCE, ES_DOE, ES_DAE, ES_SOE, ES_SAE, ES_VLD = 1, 2, 8, 0x10, 0x20, 0x40, 0x80, 1 << 31
_TCD = struct.Struct("<IhHIiIhHiHH")       # SADDR SOFF ATTR NBYTES SLAST DADDR DOFF CITER DLAST_SGA CSR BITER
_SIZES = {0: 1, 1: 2, 2: 4, 4: 16, 5: 32}


def dma_request(chip: Any, source: int) -> bool:
    """A peripheral asserts DMA request `source`. Returns True if a DMAMUX channel routes it to the DMA (the
    request is then serviced, possibly immediately); False if no enabled channel uses this source."""
    mux = getattr(chip, "_dmamux_cache", None)
    if mux is None:
        mux = [b for b in getattr(chip, "peripherals", {}).values() if getattr(b, "model", None) == "kinetis.dmamux"]
        try:
            chip._dmamux_cache = mux
        except Exception:
            pass
    routed = False
    for m in mux:
        routed |= m.request(source)
    return routed


@register("kinetis.dmamux")
class DMAMUX(GatedBlock):
    model = "kinetis.dmamux"
    gate = "DMAMUX"

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        r = [Reg(f"CHCFG{i}", i, width=1, on_write=self._chcfg_write) for i in range(NCH)]
        super().__init__(name, base, size, r, log=log, params=params)
        self.dma_name = params.get("dma", "DMA")
        self.dma: Any = None
        self._sources: dict[int, Callable[[], bool]] = {}
        self._warned_trig = False

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self.dma = find_model(chip, "kinetis.dma", self.dma_name)
        if self.dma is not None:
            self.dma.mux = self

    # ------------------------------------------------------------------ routing
    def register_source(self, source: int, active: Callable[[], bool]) -> None:
        self._sources[int(source)] = active

    def source_of(self, ch: int) -> int | None:
        """Request source routed to DMA channel ch (None if the channel is disabled)."""
        v = self.mem[ch]
        return (v & 0x3F) if v & ENBL else None

    def source_active(self, source: int) -> bool:
        if source in ALWAYS_ON:
            return True
        fn = self._sources.get(source)
        try:
            return bool(fn()) if fn is not None else False
        except Exception:
            return False

    def channels_for(self, source: int) -> list[int]:
        return [ch for ch in range(NCH) if self.mem[ch] & ENBL and (self.mem[ch] & 0x3F) == source]

    def request(self, source: int) -> bool:
        chans = self.channels_for(source)
        if not chans or self.dma is None:
            return False
        for ch in chans:
            self.dma.hw_request(ch)
        return True

    def _chcfg_write(self, blk, reg, old, new):
        ch = reg.offset
        if new & TRIG and not self._warned_trig:
            self._warned_trig = True
            self.emit("info", block=self.name, msg=f"{self.name}: CHCFG{ch}.TRIG (PIT periodic trigger) is not "
                      "modelled; requests are passed through untriggered")
        if new & ENBL and (not old & ENBL or (old ^ new) & 0x3F) and self.dma is not None:
            self._store(reg, new)
            self.dma.hw_request(ch)
        return None

    def reset(self) -> None:
        super().reset()


@register("kinetis.dma")
class DMA(GatedBlock):
    model = "kinetis.dma"
    gate = "DMA"

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.nch = int(params.get("channels", NCH))
        R: list[Reg] = [
            Reg("CR", 0x00, on_write=self._cr_write),
            Reg("ES", 0x04, ro_mask=0xFFFFFFFF),
            Reg("ERQ", 0x0C, on_write=self._erq_write),
            Reg("EEI", 0x14),
            Reg("CEEI", 0x18, 1, on_write=self._setclr("EEI", False), on_read=lambda b, r: 0),
            Reg("SEEI", 0x19, 1, on_write=self._setclr("EEI", True), on_read=lambda b, r: 0),
            Reg("CERQ", 0x1A, 1, on_write=self._setclr("ERQ", False), on_read=lambda b, r: 0),
            Reg("SERQ", 0x1B, 1, on_write=self._setclr("ERQ", True), on_read=lambda b, r: 0),
            Reg("CDNE", 0x1C, 1, on_write=self._cdne_write, on_read=lambda b, r: 0),
            Reg("SSRT", 0x1D, 1, on_write=self._ssrt_write, on_read=lambda b, r: 0),
            Reg("CERR", 0x1E, 1, on_write=self._setclr("ERR", False), on_read=lambda b, r: 0),
            Reg("CINT", 0x1F, 1, on_write=self._setclr("INT", False), on_read=lambda b, r: 0),
            Reg("INT", 0x24, w1c_mask=0xFFFFFFFF, on_write=lambda b, r, o, n: self._sync_irqs()),
            Reg("ERR", 0x2C, w1c_mask=0xFFFFFFFF, on_write=lambda b, r, o, n: self._sync_irqs()),
            Reg("HRS", 0x34, ro_mask=0xFFFFFFFF, on_read=lambda b, r: self.hrs()),
            Reg("EARS", 0x44),
        ]
        for ch in range(self.nch):
            # DCHPRIn byte order is reversed within each word (RM); reset priority = channel number
            R.append(Reg(f"DCHPRI{ch}", 0x100 + (ch & ~3) + 3 - (ch & 3), 1, reset=ch))
        for ch in range(self.nch):
            t = TCD_BASE + 32 * ch
            R.append(Reg(f"TCD{ch}_CSR", t + 0x1C, 2, on_write=self._csr_write(ch)))
        super().__init__(name, base, size, R, log=log, params=params)
        self.mux: Any = None
        self._irq: list[int | None] = [None] * self.nch
        self._irq_err: int | None = None
        self._lines = 0
        self._err_line = False
        self._busy = False
        self._queue: list[int] = []
        self.transfers = 0               # minor loops executed (statistics)

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        for ch in range(self.nch):
            self._irq[ch] = irq_number(chip, f"DMA{ch}", f"DMA{ch & 15}")
        self._irq_err = irq_number(chip, "DMA_Error", "DMA_ERROR")
        if self.mux is None:
            self.mux = find_model(chip, "kinetis.dmamux")

    def reset(self) -> None:
        if self.chip is not None:
            for ch in range(self.nch):
                if self._lines >> ch & 1 and self._irq[ch] is not None:
                    self.irq(self._irq[ch], False)
            if self._err_line and self._irq_err is not None:
                self.irq(self._irq_err, False)
        self._lines = 0
        self._err_line = False
        self._queue.clear()
        self._busy = False
        super().reset()

    # ------------------------------------------------------------------ TCD access
    def tcd(self, ch: int) -> list:
        o = TCD_BASE + 32 * ch
        return list(_TCD.unpack_from(self.mem, o))

    def _put_tcd(self, ch: int, t: list) -> None:
        _TCD.pack_into(self.mem, TCD_BASE + 32 * ch, *t)

    def _bit(self, reg: str, ch: int) -> bool:
        return bool(self.get(reg) >> ch & 1)

    def _setbit(self, reg: str, ch: int, on: bool) -> None:
        v = self.get(reg)
        self.set(reg, (v | (1 << ch)) if on else (v & ~(1 << ch)))

    def hrs(self) -> int:
        if self.mux is None:
            return 0
        v = 0
        for ch in range(self.nch):
            s = self.mux.source_of(ch)
            if s is not None and self.mux.source_active(s):
                v |= 1 << ch
        return v

    # ------------------------------------------------------------------ IRQ lines
    def _sync_irqs(self) -> None:
        if self.chip is None:
            return
        intr = self.get("INT")
        changed = intr ^ self._lines
        for ch in range(self.nch):
            if changed >> ch & 1 and self._irq[ch] is not None:
                self.irq(self._irq[ch], bool(intr >> ch & 1))
        self._lines = intr & ((1 << self.nch) - 1)
        err = bool(self.get("ERR") & self.get("EEI"))
        if err != self._err_line and self._irq_err is not None:
            self.irq(self._irq_err, err)
        self._err_line = err

    # ------------------------------------------------------------------ register hooks
    def _setclr(self, reg: str, on: bool):
        def wr(blk, r, old, new):
            if new & 0x80:                       # NOP
                return 0
            if new & 0x40:                       # all channels
                self.set(reg, ((1 << self.nch) - 1) if on else 0)
            else:
                ch = new & 0x1F
                if ch < self.nch:
                    self._setbit(reg, ch, on)
            if reg == "ERQ" and on:
                self._kick_all()
            if reg in ("INT", "ERR", "EEI"):
                self._sync_irqs()
            return 0
        return wr

    def _cr_write(self, blk, reg, old, new):
        if new & CR_CX:                          # cancel transfer: nothing is ever in flight here
            new &= ~CR_CX
        if old & CR_HALT and not new & CR_HALT:
            self._store(reg, new)
            self._kick_all()
        return new

    def _erq_write(self, blk, reg, old, new):
        if new & ~old:
            self._store(reg, new)
            self._kick_all()
        return None

    def _cdne_write(self, blk, reg, old, new):
        if new & 0x80:
            return 0
        chans = range(self.nch) if new & 0x40 else [new & 0x1F]
        for ch in chans:
            if ch < self.nch:
                t = self.tcd(ch)
                t[9] &= ~CSR_DONE
                self._put_tcd(ch, t)
        return 0

    def _ssrt_write(self, blk, reg, old, new):
        if new & 0x80:
            return 0
        chans = list(range(self.nch)) if new & 0x40 else [new & 0x1F]
        for ch in chans:
            if ch < self.nch:
                t = self.tcd(ch)
                t[9] |= CSR_START
                self._put_tcd(ch, t)
                self._enqueue(ch)
        return 0

    def _csr_write(self, ch: int):
        def wr(blk, reg, old, new):
            if new & CSR_START:                  # software start request (DONE is plain storage: write 0 clears)
                self._store(reg, new)
                self._enqueue(ch)
            return None
        return wr

    # ------------------------------------------------------------------ requests
    def hw_request(self, ch: int) -> None:
        """Hardware request on channel ch (from the DMAMUX)."""
        if ch < self.nch and self._bit("ERQ", ch):
            self._enqueue(ch)

    def _kick_all(self) -> None:
        if self.mux is None:
            return
        for ch in range(self.nch):
            if self._bit("ERQ", ch):
                s = self.mux.source_of(ch)
                if s is not None and self.mux.source_active(s):
                    self._enqueue(ch)

    def _enqueue(self, ch: int) -> None:
        self._queue.append(ch)
        if self._busy:
            return
        self._busy = True
        try:
            guard = 0
            while self._queue:
                c = self._queue.pop(0)
                if self.get("CR") & CR_HALT:
                    continue
                more = self._service(c)
                guard += 1
                # a still-active hardware request (always-on source, or a peripheral that still has data)
                if more and self._bit("ERQ", c) and self.mux is not None:
                    s = self.mux.source_of(c)
                    if s is not None and self.mux.source_active(s) and guard < 1_000_000:
                        self._queue.append(c)
        finally:
            self._busy = False

    # ------------------------------------------------------------------ the transfer engine
    def _error(self, ch: int, bits: int, msg: str) -> None:
        self.set("ES", ES_VLD | bits | (ch << 8))
        self._setbit("ERR", ch, True)
        self._setbit("ERQ", ch, False)
        self.emit("warn", block=self.name, msg=f"{self.name} channel {ch}: {msg}")
        self._sync_irqs()

    def _service(self, ch: int) -> bool:
        """Execute one minor loop of channel ch. Returns True if the channel can take further requests."""
        bus = self.chip.bus
        saddr, soff, attr, nbytes_reg, slast, daddr, doff, citer_reg, dlast, csr, biter_reg = self.tcd(ch)
        csr &= ~CSR_START
        cr = self.get("CR")
        # NBYTES / minor-loop offset
        mloff = 0
        if cr & CR_EMLM:
            smloe, dmloe = nbytes_reg >> 31 & 1, nbytes_reg >> 30 & 1
            if smloe or dmloe:
                nbytes = nbytes_reg & 0x3FF
                mloff = (nbytes_reg >> 10) & 0xFFFFF
                if mloff & 0x80000:
                    mloff -= 1 << 20
            else:
                nbytes = nbytes_reg & 0x3FFFFFFF
                smloe = dmloe = 0
        else:
            smloe = dmloe = 0
            nbytes = nbytes_reg
        ssize, dsize = _SIZES.get(attr >> 8 & 7), _SIZES.get(attr & 7)
        smod, dmod = attr >> 11 & 0x1F, attr >> 3 & 0x1F
        elink = bool(citer_reg & 0x8000)
        citer = citer_reg & (0x1FF if elink else 0x7FFF)
        biter = biter_reg & (0x1FF if biter_reg & 0x8000 else 0x7FFF)
        if ssize is None or dsize is None or nbytes == 0 or nbytes % ssize or nbytes % dsize or nbytes > 1 << 20 \
                or citer == 0 or biter == 0 or (saddr % ssize) or (daddr % dsize):
            self._put_tcd(ch, [saddr, soff, attr, nbytes_reg, slast, daddr, doff, citer_reg, dlast, csr, biter_reg])
            self._error(ch, ES_NCE | (ES_SAE if ssize and saddr % ssize else 0)
                        | (ES_DAE if dsize and daddr % dsize else 0),
                        f"configuration error (NBYTES={nbytes}, SSIZE={ssize}, DSIZE={dsize}, CITER={citer}, "
                        f"BITER={biter}, SADDR=0x{saddr:08x}, DADDR=0x{daddr:08x})")
            return False

        def modaddr(base: int, addr: int, mod: int) -> int:
            if not mod:
                return addr & 0xFFFFFFFF
            m = (1 << mod) - 1
            return ((base & ~m) | (addr & m)) & 0xFFFFFFFF

        # ---- minor loop
        buf = bytearray()
        s, d = saddr, daddr
        try:
            done = 0
            while done < nbytes:
                if ssize <= 4:
                    buf += bus.read(s, ssize).to_bytes(ssize, "little")
                else:
                    for k in range(0, ssize, 4):
                        buf += bus.read(s + k, 4).to_bytes(4, "little")
                done += ssize
                s = modaddr(saddr, s + soff, smod)
        except Exception as e:  # unmapped / bus fault
            self._put_tcd(ch, [s, soff, attr, nbytes_reg, slast, daddr, doff, citer_reg, dlast, csr, biter_reg])
            self._error(ch, ES_SBE, f"source bus error at 0x{s:08x} ({e})")
            return False
        try:
            for k in range(0, nbytes, dsize):
                chunk = buf[k:k + dsize]
                if dsize <= 4:
                    bus.write(d, dsize, int.from_bytes(chunk, "little"))
                else:
                    for j in range(0, dsize, 4):
                        bus.write(d + j, 4, int.from_bytes(chunk[j:j + 4], "little"))
                d = modaddr(daddr, d + doff, dmod)
        except Exception as e:
            self._put_tcd(ch, [s, soff, attr, nbytes_reg, slast, d, doff, citer_reg, dlast, csr, biter_reg])
            self._error(ch, ES_DBE, f"destination bus error at 0x{d:08x} ({e})")
            return False
        self.transfers += 1
        if smloe:
            s = (s + mloff) & 0xFFFFFFFF
        if dmloe:
            d = (d + mloff) & 0xFFFFFFFF
        citer -= 1
        link_minor = (citer_reg >> 9 & 0xF) if elink else None
        more = True
        if citer == 0:
            # ---- major loop complete
            csr |= CSR_DONE
            if csr & CSR_INTMAJOR:
                self._setbit("INT", ch, True)
            if csr & CSR_DREQ:
                self._setbit("ERQ", ch, False)
                more = False
            link_major = (csr >> 8 & 0xF) if csr & CSR_MAJORELINK else None
            if csr & CSR_ESG:
                try:
                    raw = bus.read_bytes(dlast & 0xFFFFFFFF, 32)
                except Exception as e:
                    self._error(ch, ES_DBE, f"scatter/gather load error at 0x{dlast & 0xFFFFFFFF:08x} ({e})")
                    return False
                nt = list(_TCD.unpack(bytes(raw)))
                nt[9] |= CSR_DONE
                self._put_tcd(ch, nt)
                if nt[9] & CSR_START:
                    self._queue.append(ch)
            else:
                s = (s + slast) & 0xFFFFFFFF
                d = (d + dlast) & 0xFFFFFFFF
                self._put_tcd(ch, [s, soff, attr, nbytes_reg, slast, d, doff, biter_reg, dlast, csr, biter_reg])
            if link_major is not None:
                self._link(link_major)
            # minor-loop link is suppressed on the final iteration in favour of the major link (RM)
            if link_minor is not None and link_major is None:
                self._link(link_minor)
            if not (csr & CSR_DREQ) and self.mux is not None and self.mux.source_of(ch) in ALWAYS_ON:
                more = False                     # always-on source: stop after a complete major loop
        else:
            csr &= ~CSR_DONE
            if csr & CSR_INTHALF and citer == biter // 2:
                self._setbit("INT", ch, True)
            new_citer = (citer_reg & ~(0x1FF if elink else 0x7FFF)) | citer
            self._put_tcd(ch, [s, soff, attr, nbytes_reg, slast, d, doff, new_citer, dlast, csr, biter_reg])
            if link_minor is not None:
                self._link(link_minor)
        self._sync_irqs()
        return more

    def _link(self, ch: int) -> None:
        if ch < self.nch:
            t = self.tcd(ch)
            t[9] |= CSR_START
            self._put_tcd(ch, t)
            self._queue.append(ch)
