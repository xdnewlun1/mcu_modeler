"""STM32F446 quad-SPI interface (QUADSPI, registers at 0xA0001000, memory-mapped window 0x90000000), checked against
RM0390 Rev 9 ch. 12 (12.3 functional description, 12.5 registers); section numbers below refer to it.

Registers (12.5.1-12.5.13): CR (EN, ABORT, DMAEN, TCEN, SSHIFT, DFM, FSEL, FTHRES[4:0], TEIE/TCIE/FTIE/SMIE/TOIE, APMS,
PMM, PRESCALER[7:0]; bits 5, 13-15, 21 reserved), DCR (CKMODE, CSHT, FSIZE), SR (TEF, TCF, FTF, SMF, TOF, BUSY,
FLEVEL; reset 0), FCR (CTEF/CTCF/CSMF/CTOF, write 1 to clear; reads 0), DLR, CCR (INSTRUCTION, IMODE, ADMODE, ADSIZE,
ABMODE, ABSIZE, DCYC, DMODE, FMODE, SIOO, DHHC, DDRM), AR, ABR, DR (the 32-byte FIFO), PSMKR, PSMAR, PIR, LPTR.  CR's
configuration fields, DCR, CCR, AR, ABR, DLR, PSMKR, PSMAR, PIR and LPTR are ignored while BUSY = 1 (`warn` once; AR
silently, see below); AR writes are also ignored in memory-mapped mode (12.5.7).

Commands (12.3.3): up to five phases -- instruction (8 bits), address (ADSIZE + 1 bytes), alternate bytes (ABSIZE + 1
bytes), DCYC dummy clocks, data -- each on 0/1/2/4 lines.  CLK = HCLK / (PRESCALER + 1) (12.5.1); DDRM halves the
address / alternate / data clocks (the instruction stays SDR, dummy cycles are whole CLK cycles).
* Start (12.3.5 "Triggering the start of a command"): indirect write with data starts on the first DR write, otherwise
  on the AR write (ADMODE != 0) or the CCR write.  BUSY rises at the start.
* Indirect write (FMODE 00): bytes leave the FIFO at the data rate; with an empty FIFO the clock stops (nCS low) until
  more data arrives.  After DLR + 1 bytes (DLR = 0xFFFFFFFF: up to the end of the 2^(FSIZE+1)-byte flash) nCS rises
  (the flash starts programming), TCF is set and bytes written beyond DL are flushed from the FIFO.
* Indirect read (01): bytes enter the FIFO at the data rate.  When the FIFO is full the read stops and restarts only
  when 4 bytes are vacant (12.3.5; the RM's "FLEVEL <= 11" belongs to a 16-byte FIFO, the model uses 32 - 4).  TCF after
  DLR + 1 bytes (DLR all ones: to the end of the flash); BUSY clears when the FIFO is empty too.  A DR read on an empty
  FIFO is answered at once with the next byte (silicon stalls the AHB until it arrives: the CPU stall is not modelled).
* FTF (12.5.1 FTHRES, 12.5.3): write mode -- at least FTHRES + 1 free bytes; read mode -- at least FTHRES + 1 bytes,
  or any byte left after the transfer completed; only while EN = 1 (SR resets to 0).  DR accesses of 8/16/32 bits
  move 1/2/4 bytes (12.5.9).
* Automatic status polling (10, 12.3.6): the command (DLR + 1 <= 4 status bytes, little-endian in DR) repeats every
  (command clocks + max(PIR, CSHT + 1)) CLK periods -- PIR is "the number of CLK cycles between two reads" (12.5.12),
  read as the nCS-high gap, which CSHT bounds from below (12.5.2).  SMF when (status & PSMKR) == (PSMAR & PSMKR) (PMM =
  0) or any unmasked bit equals its PSMAR bit (PMM = 1); APMS = 1 stops at the match (BUSY clears).  FTF is set by each
  completed status read and cleared by the DR read (12.5.3); FLEVEL reads 0.  The match time comes from the flash's
  status timeline (one event); per-poll events only while FTIE = 1.  TEF for an address outside FSIZE (12.3.13).
* Memory-mapped (11, 12.3.7): the CCR write maps min(2^(FSIZE+1), 256 MB) bytes at 0x90000000 (Bus.map_window).
  When the read command returns plain array data (QspiNorFlash.can_map: right lines, right clocks before the data, QE
  set, flash idle), TCEN = 0, DDRM = 0 and DFM = 0, the flash array itself is mapped (fast, executable: XIP); BUSY and
  nCS then behave as after the first access from the CCR write on (no access is observable).  Otherwise every access
  goes through a callback (not executable): BUSY rises and nCS falls at the first access; with TCEN = 1, TOF is set,
  BUSY falls and nCS rises LPTR CLK cycles after the FIFO filled with 32 prefetched bytes following the last access
  (12.5.13).  Accesses past FSIZE, or with the window unmapped, are bus errors.  Writes are ignored (`warn`).  Access
  latency and the prefetch itself are not timed (accesses take no time).  DR reads 0 (12.3.11).
* TEF (12.3.13): an indirect command whose address, or address + data length, exceeds the flash sets TEF and does not
  run; same for the automatic polling address.
* ABORT (12.3.14): stops the command (a program/erase write command releases nCS: the flash executes what it got),
  flushes the FIFO, sets TCF (12.5.3: in any mode), clears BUSY and ABORT; in memory-mapped mode nCS rises and BUSY
  falls until the next access.  EN = 0 also stops a running command and unmaps the window.
* SIOO (12.3.12): the instruction phase is sent only with the first command after a CCR write; the later commands are
  shorter by the instruction clocks.  The flash model has no continuous-read mode: it still decodes the instruction
  (`warn` once).
* Dual-flash mode (DFM, 12.3.4): flash 1 (BK1) and flash 2 (BK2) receive the same instruction / address (address / 2)
  / alternate bytes; data byte j of the phase is flash 1's when j is even and flash 2's when odd (status bytes
  interleave the same way), 8 data lines (twice the byte rate); writes split the bytes; BK2_NCS mirrors BK1_NCS
  (12.3.15); DL[0] reads 1 and ADDRESS[0] reads 0 (12.5.5 / 12.5.7).  FSEL is ignored with DFM.
* Not modelled: CKMODE (CLK idle level), DHHC, SSHIFT and the half-cycle sampling (no signal delays exist in the model;
  12.3.9: SSHIFT with DDRM logs a `warn`, as does DDRM / DHHC with PRESCALER = 0, 12.5.1 / 12.5.6); CSHT between two
  indirect commands; DDR data on the flash side (the flash model has no DTR commands: data are exchanged as in SDR).
* IRQ "QUADSPI" (92) = (TEF & TEIE) | (TCF & TCIE) | (FTF & FTIE) | (SMF & SMIE) | (TOF & TOIE) (12.4); DMA request
  "QUADSPI" (DMA2 stream 7 channel 3, RM0390 Table 29) while FTF & DMAEN in indirect mode.  Flag times are exact: one
  scheduler event per FIFO threshold crossing / completion (only when an interrupt or the DMA is enabled for FTF),
  flags otherwise computed lazily when SR is read.
* Pins: the command reaches the flash only when QUADSPI_CLK, QUADSPI_BKn_NCS and the data lines it uses are muxed to
  the QUADSPI (otherwise reads give 0xFF, writes are lost: `warn`).  nCS is driven low for each indirect command and
  for a polling phase (not toggled per poll); CLK is summarised as a gated `wave` (chipem/periph/sdbus.py ClockWave).
* Flash devices: `attach_qspi_flash(dev, bank)` (board `devices: [{type: spi_flash, spi: QUADSPI, bank: 2, ...}]` or
  `--spi-flash QUADSPI:...`, which build a chipem.devices.qspi_flash.QspiNorFlash).
* RCC_AHB3ENR.QSPIEN gate (gated access: one `warn`); RCC_AHB3RSTR.QSPIRST resets the block.
"""
from __future__ import annotations

from typing import Any

from ..base import pure_read
from ..registry import register
from ..sdbus import ClockWave, lines_muxed
from ._util import GatedBlock, build_regs, dma_request, irq_number

MM_BASE = 0x90000000
MM_MAX = 256 << 20                      # 12.3.7: memory-mapped space
_ADDRESSED = {0x03, 0x0B, 0x3B, 0xBB, 0x6B, 0xEB}       # memory reads: data continue at addr + offset
FIFO_SIZE = 32
# CR
EN, ABORT, DMAEN, TCEN, SSHIFT, DFM, FSEL = 1 << 0, 1 << 1, 1 << 2, 1 << 3, 1 << 4, 1 << 6, 1 << 7
TEIE, TCIE, FTIE, SMIE, TOIE, APMS, PMM = (1 << b for b in (16, 17, 18, 19, 20, 22, 23))
CR_CFG = 0xFFC000D8                      # PRESCALER, PMM, APMS, FSEL, DFM, SSHIFT, TCEN: only while BUSY = 0
# SR
TEF, TCF, FTF, SMF, TOF, BUSY = 1, 2, 4, 8, 16, 32
STICKY = TEF | TCF | SMF | TOF
LINES = (0, 1, 2, 4)


class _Op:
    """A running command."""
    __slots__ = ("fmode", "instr", "lines", "adbytes", "addr", "gap", "dl", "n", "left", "t_next", "t_byte",
                 "last_t", "status", "shift", "wbuf", "start", "clocks", "dev", "off", "done", "poll", "stall",
                 "dfm", "devs")

    def __init__(self) -> None:
        self.wbuf = bytearray()
        self.done = False
        self.off = 0
        self.last_t = 0.0
        self.poll: dict | None = None
        self.stall = False                      # indirect read: FIFO full, clock stopped until 4 bytes are free
        self.dfm = False                        # dual-flash mode: bytes interleaved over flash 1 / flash 2
        self.devs: dict[int, tuple[Any, str, int]] = {}     # DFM: bank -> (device, check status, shift)


@register("stm32.quadspi")
class QUADSPI(GatedBlock):
    model = "stm32.quadspi"

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.gate = params.get("gate", "QUADSPI")
        prot = {"on_write": self._prot_write}
        ov: dict[str, dict] = {
            "CR": {"on_write": self._cr_write},
            "DCR": prot, "DLR": prot, "ABR": prot, "PSMKR": prot, "PSMAR": prot, "PIR": prot, "LPTR": prot,
            "CCR": {"on_write": self._ccr_write}, "AR": {"on_write": self._ar_write},
            "SR": {"ro_mask": 0xFFFFFFFF, "on_read": self._sr_read},
            "FCR": {"on_write": self._fcr_write, "on_read": pure_read(lambda b, r: 0)},
        }
        r = build_regs((name, "QUADSPI"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.flash: dict[int, Any] = {}
        self.fifo = bytearray()
        self.op: _Op | None = None
        self.mm: str | None = None             # None | "fast" | "slow" (mapped window kind)
        self._mm_busy = False
        self._mm_tbyte = 0.0
        self._ev = None
        self._tof_ev = None
        self._irqn: int | None = None
        self._line = False
        self._dreq = False
        self._warned: set[str] = set()
        self._sioo_sent = False                 # CCR.SIOO: the instruction went out since the last CCR write
        self.wave = ClockWave(self, "QUADSPI_CLK")
        self.stats = {"commands": 0, "bytes_in": 0, "bytes_out": 0, "polls": 0}

    # ------------------------------------------------------------------ setup
    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irqn = irq_number(chip, self.name, "QUADSPI")

    def attach_qspi_flash(self, dev: Any, bank: int = 1) -> None:
        bank = 2 if int(bank) == 2 else 1
        self.flash[bank] = dev
        dev.emit_fn = lambda kind, **f: self.emit(kind, block=self.name, **f)

    def reset(self) -> None:
        self._stop_events()
        self._mm_close()
        if self.op is not None:
            self._ncs(False)
        self.op = None
        self.fifo.clear()
        super().reset()
        self.wave.reset()
        self._warned.clear()
        self._sioo_sent = False
        if self._line and self.chip is not None and self._irqn is not None:
            self.irq(self._irqn, False)
        self._line = False
        if self._dreq and self.chip is not None:
            dma_request(self.chip, "QUADSPI", False)
        self._dreq = False

    def _stop_events(self) -> None:
        if self.chip is not None:
            for ev in (self._ev, self._tof_ev):
                if ev is not None:
                    self.chip.sched.cancel(ev)
        self._ev = None
        self._tof_ev = None

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=f"{self.name}: {msg}")

    # ------------------------------------------------------------------ helpers
    def _now(self) -> float:
        return self.chip.now_ns() if self.chip is not None else 0.0

    def _f(self, reg: str, lsb: int, width: int) -> int:
        return (self.get(reg) >> lsb) & ((1 << width) - 1)

    def clk_hz(self) -> float:
        hclk = float(self.chip.clock.hz) if self.chip is not None else 16e6
        return hclk / (self._f("CR", 24, 8) + 1)

    def flash_bytes(self) -> int:
        return 1 << (self._f("DCR", 16, 5) + 1)

    def _thr(self) -> int:
        return self._f("CR", 8, 5) + 1

    def _busy(self) -> bool:
        op = self.op
        if self.mm is not None and self._mm_busy:
            return True
        if op is None:
            return False
        if op.done:
            return op.fmode == 1 and bool(self.fifo)
        return True

    def _bank(self) -> int:
        return 2 if self.get("CR") & FSEL else 1

    def _dfm(self) -> bool:
        return bool(self.get("CR") & DFM)

    def _signals(self, dlines: int) -> list[str]:
        n = max(dlines, 1)
        if self._dfm():
            # 12.3.15: in dual-flash mode BK2_NCS mirrors BK1_NCS; BK1_NCS alone may select both flashes
            return (["QUADSPI_CLK", "QUADSPI_BK1_NCS"] + [f"QUADSPI_BK1_IO{i}" for i in range(n)]
                    + [f"QUADSPI_BK2_IO{i}" for i in range(n)])
        b = self._bank()
        return ["QUADSPI_CLK", f"QUADSPI_BK{b}_NCS"] + [f"QUADSPI_BK{b}_IO{i}" for i in range(n)]

    def _ncs(self, low: bool, t: float | None = None) -> None:
        board = getattr(self.chip, "pins", None) if self.chip is not None else None
        if board is None:
            return
        # 12.3.15: FSEL = 0 -> only BK1_NCS moves, FSEL = 1 -> only BK2_NCS; DFM = 1 -> both
        for b in ((1, 2) if self._dfm() else (self._bank(),)):
            board.drive_signal(f"QUADSPI_BK{b}_NCS", "L" if low else "H")

    # ------------------------------------------------------------------ registers
    def _prot_write(self, blk, reg, old, new):
        if self._busy():
            if new != old:
                self._warn_once(f"busy{reg.name}", f"{reg.name} written while BUSY = 1: ignored (abort first)")
            return old
        if reg.name == "DLR" and self._dfm():
            new |= 1                            # 12.5.5: DL[0] stuck at 1 in dual-flash mode (even byte counts)
            self._store(reg, new)
        if reg.name == "DCR" and self.mm is not None and (old ^ new) >> 16 & 0x1F:
            self._store(reg, new)
            self._mm_open()
        return new

    def _cr_write(self, blk, reg, old, new):
        busy = self._busy()
        if busy and (old ^ new) & CR_CFG:
            self._warn_once("busycr", "CR configuration fields (PRESCALER, PMM, APMS, FSEL, DFM, SSHIFT, TCEN) written "
                            "while BUSY = 1: kept")
            new = (new & ~CR_CFG) | (old & CR_CFG)
        if new & DFM and not old & DFM:
            # 12.5.5 / 12.5.7: DL[0] reads 1 and ADDRESS[0] reads 0 while DFM = 1
            self._store(self.regs["DLR"], self.get("DLR") | 1)
            self._store(self.regs["AR"], self.get("AR") & ~1)
        if new & ABORT:
            self._abort()
            new &= ~ABORT
            self._store(reg, new)
        if old & EN and not new & EN:
            self._stop("EN = 0")
            self._mm_close()
        elif new & EN and not old & EN and self.mm is None and self._fmode() == 3:
            self._mm_open()
        self.wave.set(self.clk_hz() if new & EN else 0.0, gated=True)
        self._update()
        return new

    def _fmode(self) -> int:
        return self._f("CCR", 26, 2)

    def _ccr_write(self, blk, reg, old, new):
        if self._busy():
            self._warn_once("busyCCR", "CCR written while BUSY = 1: ignored (abort first)")
            return old
        self._store(reg, new)
        self._sioo_sent = False                 # 12.3.12: the next command sends the instruction again
        fm = (new >> 26) & 3
        if fm != 3:
            self._mm_close()
        if self.op is not None and self.op.done:
            self.op = None
        adm, dm = (new >> 10) & 3, (new >> 24) & 3
        if fm == 3:
            self._mm_open()
        elif adm == 0 and (fm in (1, 2) or dm == 0):
            self._start()
        self._update()
        return None

    def _ar_write(self, blk, reg, old, new):
        ccr = self.get("CCR")
        fm, adm, dm = (ccr >> 26) & 3, (ccr >> 10) & 3, (ccr >> 24) & 3
        if self._busy() or fm == 3:
            # 12.5.7: ignored while BUSY = 1 or in memory-mapped mode (no warning: HAL_QSPI_AutoPolling clears AR
            # right after the CCR write that started the polling command)
            return old
        if self._dfm():
            new &= ~1                           # 12.5.7: ADDRESS[0] stuck at 0 in dual-flash mode
        self._store(reg, new)
        if self.op is not None and self.op.done:
            self.op = None
        if adm and (fm in (1, 2) or (fm == 0 and dm == 0)):
            self._start()
        self._update()
        return None

    def _fcr_write(self, blk, reg, old, new):
        sr = self.regs["SR"]
        v = self.get("SR") & ~(new & STICKY)
        self._store(sr, v)
        self._update()
        return 0

    def _sr_read(self, blk, reg):
        now = self._now()
        op = self.op
        self._advance(now)
        if op is not None and self.op is op and op.done:
            self._update()                      # completion seen first by a poll: IRQ / DMA lines follow at once
        return self._sr_value()

    def _sr_value(self) -> int:
        v = self.get("SR") & STICKY
        if self._ftf():
            v |= FTF
        if self._busy():
            v |= BUSY
        return v | (min(len(self.fifo), FIFO_SIZE) << 8)

    def _ftf(self) -> bool:
        if not self.get("CR") & EN:
            return False                        # 12.5.3: SR resets to 0 (FTF only once the interface is enabled)
        op = self.op
        fm = self._fmode()
        if fm == 2:
            # 12.3.6 / 12.5.3: set each time a status read completes, cleared when DR is read
            return op is not None and op.poll is not None and self._poll_k(self._now()) > op.poll["read_k"]
        if fm == 0:
            if op is not None and op.done:
                return False
            return FIFO_SIZE - len(self.fifo) >= self._thr()
        if fm == 1:
            return len(self.fifo) >= self._thr() or (op is not None and op.done and bool(self.fifo))
        return False

    def _set(self, bits: int) -> None:
        self._store(self.regs["SR"], self.get("SR") | bits)

    # ------------------------------------------------------------------ DR (FIFO)
    def read(self, offset: int, size: int) -> int:
        if 0x20 <= offset < 0x24:
            self._gate_ok()
            return self._dr_read(size)
        if offset == 0x08:
            self._gate_ok()
        return super().read(offset, size)

    def write(self, offset: int, size: int, value: int) -> None:
        if 0x20 <= offset < 0x24:
            self._gate_ok()
            self._dr_write(size, value)
            return
        self._gate_ok()
        super().write(offset, size, value)

    def _dr_read(self, size: int) -> int:
        now = self._now()
        self._advance(now)
        op = self.op
        if op is not None and op.fmode == 2 and op.poll is not None:
            v = self._poll_value(now) & ((1 << (8 * size)) - 1)
            op.poll["read_k"] = self._poll_k(now)                # 12.5.3: reading DR clears FTF
            self._update()
            self._plan()
            return v
        if self._fmode() != 1:
            return 0                                             # 12.3.11: DR reads 0 in memory-mapped mode
        out = bytearray()
        for _ in range(size):
            if not self.fifo and op is not None and not op.done:
                # empty FIFO: silicon stalls the AHB until the next byte arrives; here it is delivered at once
                if op.stall:
                    op.stall = False
                    op.t_next = max(op.t_next, now + op.t_byte)
                self._advance(max(now, op.t_next))
            if not self.fifo:
                break
            out.append(self.fifo.pop(0))
            if op is not None and op.stall and FIFO_SIZE - len(self.fifo) >= 4:
                # 12.3.5: after the FIFO filled up the flash read restarts once 4 bytes are vacant
                op.stall = False
                op.t_next = max(op.t_next, now + op.t_byte)
        self._advance(now)
        self._update()
        self._plan()
        return int.from_bytes(bytes(out) + b"\0" * (size - len(out)), "little")

    def _dr_write(self, size: int, value: int) -> None:
        now = self._now()
        if self._fmode() != 0:
            self._warn_once("drw", "DR written outside indirect write mode: ignored")
            return
        self._advance(now)
        data = (value & ((1 << (8 * size)) - 1)).to_bytes(size, "little")
        op = self.op
        for b in data:
            if len(self.fifo) >= FIFO_SIZE:
                if op is None or op.done:
                    self._warn_once("fifofull", "DR write with a full FIFO and no command running: byte lost")
                    break
                self._advance(max(now, op.t_next))             # silicon stalls the write until a byte leaves
                if len(self.fifo) >= FIFO_SIZE:
                    break
            stalled = op is not None and not op.done and not self.fifo and op.t_next <= now
            self.fifo.append(b)
            if stalled:
                op.t_next = now + op.t_byte
        ccr = self.get("CCR")
        if (op is None or op.done) and (ccr >> 24) & 3:
            self._start()
        self._advance(now)
        self._update()
        self._plan()

    # ------------------------------------------------------------------ commands
    def _start(self) -> None:
        chip = self.chip
        if chip is None:
            return
        now = self._now()
        if not self.get("CR") & EN:
            self._warn_once("en", "command written while CR.EN = 0: not started")
            return
        ccr, cr = self.get("CCR"), self.get("CR")
        op = _Op()
        op.fmode = (ccr >> 26) & 3
        imode, adm, abm, dm = (ccr >> 8) & 3, (ccr >> 10) & 3, (ccr >> 14) & 3, (ccr >> 24) & 3
        ddr = bool(ccr >> 31)
        il, al, abl, dl = LINES[imode], LINES[adm], LINES[abm], LINES[dm]
        op.instr = ccr & 0xFF if imode else None
        op.adbytes = ((ccr >> 12) & 3) + 1 if adm else 0
        op.addr = self.get("AR") if adm else 0
        abbytes = ((ccr >> 16) & 3) + 1 if abm else 0
        dcyc = (ccr >> 18) & 0x1F
        div = 2 if ddr else 1
        op.dfm = bool(cr & DFM)
        ab_clk = (8 * abbytes / abl / div) if abm else 0
        op.gap = int(round(ab_clk)) + dcyc
        # 12.3.12 SIOO: the instruction goes out only with the first command after a CCR write
        skip_instr = bool(imode and ccr >> 28 & 1 and self._sioo_sent)
        if skip_instr:
            self._warn_once("sioo", "CCR.SIOO: instruction phase skipped after the first command; the flash's "
                            "continuous-read mode is not modelled, so the flash still decodes the instruction")
        clocks = ((8 / il if imode and not skip_instr else 0) + ((8 * op.adbytes / al / div) if adm else 0)
                  + ab_clk + dcyc)
        op.dl = dl
        op.lines = (il, al, dl)
        cpb = 8 / dl / div / (2 if op.dfm else 1) if dl else 0     # DFM: 8 data lines (12.3.4)
        T = 1e9 / self.clk_hz()
        op.t_byte = cpb * T
        op.start = now
        op.clocks = clocks
        dlr = self.get("DLR")
        fsize = self.flash_bytes()
        if not dm:
            op.n = 0
        elif dlr == 0xFFFFFFFF:
            # 12.3.5: undefined length -> up to the end of the flash (FSIZE), in both indirect directions
            op.n = max(fsize - (op.addr % fsize), 0) if op.fmode in (0, 1) else None
        else:
            op.n = dlr + 1
        op.left = op.n
        self._mode_checks(ccr, cr)
        # 12.3.13: TEF for an address outside FSIZE (indirect and automatic polling) or, in indirect mode, when the
        # address plus the data length exceed the flash
        over = adm and (op.addr >= fsize or (op.fmode in (0, 1) and dm and dlr != 0xFFFFFFFF
                                             and op.addr + dlr + 1 > fsize))
        if over and op.fmode in (0, 1, 2):
            self._set(TEF)
            self._warn_once("tef", f"address 0x{op.addr:X} (+ {0 if dlr == 0xFFFFFFFF else dlr + 1} bytes) outside "
                            f"the {fsize}-byte flash (DCR.FSIZE): TEF, command not sent")
            self._update()
            return
        muxed = lines_muxed(chip, self._signals(max(il, al, abl, dl)))
        banks = (1, 2) if op.dfm else (self._bank(),)
        for b in banks:
            dev = self.flash.get(b)
            if dev is not None and not muxed:
                self._warn_once("pins", f"QUADSPI_CLK / BK{b}_NCS / IO lines not muxed to {self.name}: the flash does "
                                "not see the command (reads 0xFF)")
                dev = None
            elif dev is None:
                self._warn_once(f"nodev{b}", f"no flash attached on bank {b}: reads 0xFF (pulled-up lines)")
            st, sh = ("ignored", 0)
            if dev is not None:
                st, sh = dev.check(op.instr, op.lines, op.gap, op.adbytes, now)
            op.devs[b] = (dev, st, sh)
        op.dev, op.status, op.shift = op.devs[banks[0]]
        if imode:
            self._sioo_sent = True
        self.op = op
        self.stats["commands"] += 1
        self._ncs(True)
        t0 = now + clocks * T
        if op.fmode == 2:
            self._poll_start(op, T, dlr)
            return
        op.t_next = t0 + op.t_byte
        if op.n == 0:
            op.t_next = t0
        self.wave.active(clocks)
        self._advance(now)
        self._update()
        self._plan()

    def _mode_checks(self, ccr: int, cr: int) -> None:
        """Configuration rules of 12.3.9 / 12.5.1 / 12.5.6 (silicon misbehaves; the model warns and goes on)."""
        ddr = bool(ccr >> 31)
        if ddr:
            self._warn_once("ddr", "DDR mode: address/alternate/data phases take half the clocks; the data itself is "
                            "exchanged as with SDR (the flash model has no DTR commands)")
            if not cr >> 24:
                self._warn_once("ddrpre", "DDRM = 1 with PRESCALER = 0: DDR mode needs a clock division of two or more "
                                "(RM0390 12.5.1)")
            if cr & SSHIFT:
                self._warn_once("ddrsshift", "SSHIFT = 1 in DDR mode: the firmware must clear SSHIFT when DDRM = 1 "
                                "(RM0390 12.3.9)")
        if ccr >> 30 & 1 and not cr >> 24:
            self._warn_once("dhhc", "DHHC = 1 requires PRESCALER > 0 (RM0390 12.5.6)")

    @staticmethod
    def _dev_bytes(dev: Any, st: str, shift: int, instr: int | None, addr: int, off: int, k: int, t: float) -> bytes:
        """k data bytes of one flash, starting at byte `off` of its data phase."""
        if dev is None or st != "ok" or k <= 0:
            return b"\xff" * max(k, 0)
        if instr not in _ADDRESSED:                              # ID / status: the byte sequence restarts at 0
            return dev.read_data(instr, addr, off + k, shift, t)[off:]
        if shift < 0 and off > 0:
            return dev.read_data(instr, addr + off - 1, k + 1, shift, t)[1:]
        return dev.read_data(instr, addr + off, k, shift, t)

    def _dfm_bytes(self, op: _Op, off: int, k: int, t: float, addr: int | None = None) -> bytes:
        """Dual-flash data (12.3.4): byte j of the data phase comes from flash 1 when j is even, flash 2 when odd,
        both read from address / 2 (status bytes interleave the same way)."""
        a = (op.addr if addr is None else addr) // 2
        i0, i1 = off // 2, (off + k + 1) // 2
        part = {}
        for b in (1, 2):
            dev, st, sh = op.devs.get(b, (None, "ignored", 0))
            part[b] = self._dev_bytes(dev, st, sh, op.instr, a, i0, i1 - i0, t)
        return bytes(part[1 + (j & 1)][j // 2 - i0] for j in range(off, off + k))

    def _fetch(self, op: _Op, k: int, t: float) -> bytes:
        data = (self._dfm_bytes(op, op.off, k, t) if op.dfm else
                self._dev_bytes(op.dev, op.status, op.shift, op.instr, op.addr, op.off, k, t))
        op.off += k
        return data

    def _advance(self, now: float) -> None:
        op = self.op
        if op is None or op.done:
            return
        if op.fmode == 2:
            self._poll_advance(now)
            return
        if op.n == 0:                                            # no data phase
            if op.t_next <= now:
                self._finish(op, op.t_next)
            return
        tb = op.t_byte
        if op.fmode == 1:
            while op.left and not op.stall and op.t_next <= now:
                k = min(op.left, FIFO_SIZE - len(self.fifo), int((now - op.t_next) // tb) + 1)
                if k <= 0:
                    op.stall = True
                    break
                self.fifo += self._fetch(op, k, op.t_next)
                op.left -= k
                op.last_t = op.t_next + (k - 1) * tb
                op.t_next += k * tb
                self.stats["bytes_in"] += k
                self.wave.active(k * 8 / max(op.dl, 1) / (2 if op.dfm else 1))
                if len(self.fifo) >= FIFO_SIZE and op.left:
                    op.stall = True             # 12.3.5: FIFO full -> reading stops (CLK held low)
            if op.left == 0:
                self._finish(op, op.last_t)
        else:
            while (op.left is None or op.left) and self.fifo and op.t_next <= now:
                k = min(len(self.fifo), int((now - op.t_next) // tb) + 1)
                if op.left is not None:
                    k = min(k, op.left)
                op.wbuf += self.fifo[:k]
                del self.fifo[:k]
                if op.left is not None:
                    op.left -= k
                op.last_t = op.t_next + (k - 1) * tb
                op.t_next += k * tb
                self.stats["bytes_out"] += k
                self.wave.active(k * 8 / max(op.dl, 1) / (2 if op.dfm else 1))
            if op.left == 0:
                self._finish(op, op.last_t)

    def _execute(self, op: _Op, t: float) -> None:
        """A write / no-data command ends at t: the flash(es) act on what they received."""
        if op.dfm:
            for b, (dev, st, _sh) in op.devs.items():
                if dev is not None and st == "ok":
                    # 12.3.4: even bytes go to flash 1, odd bytes to flash 2; each sees address / 2
                    dev.execute(op.instr, op.addr // 2 if op.adbytes else None, bytes(op.wbuf[b - 1::2]), t)
        elif op.dev is not None and op.status == "ok":
            op.dev.execute(op.instr, op.addr if op.adbytes else None, bytes(op.wbuf), t)

    def _finish(self, op: _Op, t: float) -> None:
        """Last data clock done at t: chip select released, TCF."""
        op.done = True
        self._ncs(False)
        if op.fmode == 0:
            self._execute(op, t)
            self.fifo.clear()                   # 12.3.5: bytes beyond DL are flushed when TCF is set
        self._set(TCF)

    def _stop(self, why: str) -> None:
        op = self.op
        if op is None or op.done:
            return
        now = self._now()
        self._advance(now)
        if op.done:
            return
        op.done = True
        self._ncs(False)
        if op.fmode == 2 and op.poll is not None:
            self.stats["polls"] += op.poll.get("count", 0)
        if op.fmode == 0 and (op.wbuf or op.n == 0 or op.n is None):
            self._execute(op, now)
        if why:
            self.emit("info", block=self.name, msg=f"{self.name}: command 0x{(op.instr or 0):02X} stopped ({why})")

    def _abort(self) -> None:
        self._stop("")
        self._stop_events()
        self.fifo.clear()
        self.op = None
        if self.mm is not None:
            self._mm_idle()                     # memory-mapped mode stays selected; the next access restarts it
        self._set(TCF)                          # 12.5.3: TCF is set in any mode when a transfer is aborted

    # ------------------------------------------------------------------ automatic status polling
    def _poll_start(self, op: _Op, T: float, dlr: int) -> None:
        nbytes = min(dlr + 1, 4)
        if dlr + 1 > 4:
            self._warn_once("polln", f"automatic polling with DLR + 1 = {dlr + 1} bytes: only 4 are compared")
        c = op.clocks + nbytes * (op.t_byte / T if op.dl else 0)
        gap = max(self._f("PIR", 0, 16), self._f("DCR", 8, 3) + 1)
        P = (c + gap) * T
        t_first = op.start + c * T
        mask, match = self.get("PSMKR"), self.get("PSMAR")
        or_mode = bool(self.get("CR") & PMM)

        def value(t: float) -> int:
            if op.dfm:                          # 12.3.4: flash 1 / flash 2 status bytes interleaved
                return int.from_bytes(self._dfm_bytes(op, 0, nbytes, t), "little")
            return int.from_bytes(self._dev_bytes(op.dev, op.status, 0, op.instr, op.addr, 0, nbytes, t), "little")

        def ok(v: int) -> bool:
            m = mask & ((1 << (8 * nbytes)) - 1)
            if or_mode:
                return bool(~(v ^ match) & m)
            return (v & m) == (match & m)
        op.poll = {"P": P, "t_first": t_first, "value": value, "clocks": c, "k_match": None, "count": 0,
                   "read_k": -1}
        # the flash status is piecewise constant (one change per device at busy_until_ns): only the first poll and the
        # first poll after each change can be the first match
        cands = {0}
        for dev, st, _sh in op.devs.values():
            if dev is not None and st == "ok" and dev.busy_until_ns > t_first:
                cands.add(max(0, -int(-(dev.busy_until_ns - t_first) // P)))
        k = next((k2 for k2 in sorted(cands) if ok(value(t_first + k2 * P))), None)
        op.poll["k_match"] = k
        self._update()
        self._plan()                            # match event, and one event per status read while FTIE = 1

    def _poll_advance(self, now: float) -> None:
        op = self.op
        p = op.poll
        k = p["k_match"]
        if k is None or op.done:
            return
        t = p["t_first"] + k * p["P"]
        if t > now or self.get("SR") & SMF and p.get("matched"):
            return
        p["matched"] = True
        self._set(SMF)
        if self.get("CR") & APMS:
            p["count"] = k + 1
            self.wave.active(p["clocks"] * (k + 1))
            op.done = True
            self.stats["polls"] += k + 1
            self._ncs(False)

    def _poll_k(self, now: float) -> int:
        """Index of the last completed status read at `now` (-1: none yet)."""
        op = self.op
        p = op.poll if op is not None else None
        if p is None or now < p["t_first"]:
            return -1
        k = int((now - p["t_first"]) // p["P"])
        if p["k_match"] is not None and self.get("CR") & APMS:      # polling stops at the match
            k = min(k, p["k_match"])
        return k

    def _poll_value(self, now: float) -> int:
        op = self.op
        p = op.poll
        k = self._poll_k(now)
        if k < 0:
            return self.get("DR")
        return p["value"](p["t_first"] + k * p["P"])

    # ------------------------------------------------------------------ memory-mapped mode
    def _mm_open(self) -> None:
        chip = self.chip
        if chip is None or not self.get("CR") & EN:
            return
        self._mm_close()
        ccr, cr = self.get("CCR"), self.get("CR")
        imode, adm, abm, dm = (ccr >> 8) & 3, (ccr >> 10) & 3, (ccr >> 14) & 3, (ccr >> 24) & 3
        size = min(self.flash_bytes(), MM_MAX)          # 12.3.7: at most 256 Mbytes are addressable
        dfm = bool(cr & DFM)
        dev = None if dfm else self.flash.get(self._bank())
        il, al, abl, dl = LINES[imode], LINES[adm], LINES[abm], LINES[dm]
        ddr = bool(ccr >> 31)
        abbytes = ((ccr >> 16) & 3) + 1 if abm else 0
        gap = int(round(8 * abbytes / abl / (2 if ddr else 1))) if abm else 0
        gap += (ccr >> 18) & 0x1F
        instr = ccr & 0xFF if imode else None
        adbytes = ((ccr >> 12) & 3) + 1 if adm else 0
        name = f"{self.name}_MM"
        self._mode_checks(ccr, cr)
        if size < chip.bus.page:
            self._warn_once("fsize", f"memory-mapped window of {size} bytes (DCR.FSIZE) is smaller than a core page: "
                            f"{chip.bus.page} bytes mapped")
            size = chip.bus.page
        muxed = lines_muxed(chip, self._signals(max(il, al, abl, dl)))
        # T per data byte, for the prefetch fill time (12.5.13: the timeout counts from the FIFO being full)
        self._mm_tbyte = (8 / max(dl, 1) / (2 if ddr else 1) / (2 if dfm else 1)) * 1e9 / self.clk_hz()
        fast = (dev is not None and muxed and not cr & TCEN and not ddr
                and dev.size % chip.bus.page == 0 and (size % dev.size == 0 or dev.size % size == 0)
                and dev.can_map(instr, (il, al, dl), gap, adbytes, self._now()))
        if fast:
            chip.bus.map_window(name, MM_BASE, size, buffer=dev.mem, period=min(size, dev.size), writable=False)
            self.mm = "fast"
            # no access is seen on a direct mapping: BUSY and NCS behave as after the first access (12.3.7)
            self._mm_busy = True
            self._ncs(True)
        else:
            devs = {b: (self.flash.get(b) if muxed else None) for b in ((1, 2) if dfm else (self._bank(),))}
            cfg = (instr, (il, al, dl), gap, adbytes, devs, dfm)
            chip.bus.map_window(name, MM_BASE, size, read=self._mm_read(cfg), write=self._mm_write)
            self.mm = "slow"
            self._mm_busy = False               # 12.3.7: BUSY rises with the first memory-mapped access
        self.emit("info", block=self.name, msg=f"{self.name}: memory-mapped mode, {size} bytes at 0x{MM_BASE:08X} "
                  f"({'direct array mapping' if fast else 'per-access command'})")

    def _mm_idle(self) -> None:
        """Prefetch stopped (timeout or abort): NCS released, BUSY low (12.3.14)."""
        if self._mm_busy:
            self._ncs(False)
        self._mm_busy = False
        if self._tof_ev is not None and self.chip is not None:
            self.chip.sched.cancel(self._tof_ev)
        self._tof_ev = None

    def _mm_close(self) -> None:
        if self.mm is not None and self.chip is not None:
            self.chip.bus.unmap_window(f"{self.name}_MM")
            self._mm_idle()
        self.mm = None
        self._mm_busy = False
        if self._tof_ev is not None and self.chip is not None:
            self.chip.sched.cancel(self._tof_ev)
            self._tof_ev = None

    def _mm_read(self, cfg):
        instr, lines, gap, adbytes, devs, dfm = cfg

        def one(dev, a: int, n: int, now: float) -> bytes:
            if dev is None:
                return b"\xff" * n
            st, shift = dev.check(instr, lines, gap, adbytes, now)
            return self._dev_bytes(dev, st, shift, instr, a, 0, n, now)

        def rd(off: int, n: int) -> int:
            now = self._now()
            if not self._mm_busy:
                self._mm_busy = True            # first access: the prefetch starts, NCS stays low (12.3.7)
                self._ncs(True)
            if dfm:
                # 12.3.4: address X -> byte X / 2 of flash 1 (X even) or flash 2 (X odd)
                lo, hi = off // 2, (off + n + 1) // 2
                d = {b: one(devs.get(b), lo, hi - lo, now) for b in (1, 2)}
                v = int.from_bytes(bytes(d[1 + (j & 1)][j // 2 - lo] for j in range(off, off + n)), "little")
            else:
                v = int.from_bytes(one(next(iter(devs.values())), off, n, now), "little")
            if self.get("CR") & TCEN and self.chip is not None:
                if self._tof_ev is not None:
                    self.chip.sched.cancel(self._tof_ev)
                T = 1e9 / self.clk_hz()
                # 12.5.13: TIMEOUT CLK cycles after the FIFO became full with prefetched data
                fill = FIFO_SIZE * self._mm_tbyte
                self._tof_ev = self.chip.sched.at_ns(now + fill + self._f("LPTR", 0, 16) * T, self._timeout)
            return v
        return rd

    def _mm_write(self, off: int, n: int, value: int) -> None:
        self._warn_once("mmw", "write to the memory-mapped QUADSPI window: ignored (memory-mapped mode is read only)")

    def _timeout(self) -> None:
        self._tof_ev = None
        self._set(TOF)                          # 12.5.3 TOF; BUSY falls and NCS is released (12.3.14)
        self._mm_idle()
        self._update()

    # ------------------------------------------------------------------ events, IRQ, DMA
    def _tick(self) -> None:
        self._ev = None
        # events fire on whole core cycles: accept phase ends up to one core cycle ahead (no rescheduling loop)
        self._advance(self._now() + (1e9 / float(self.chip.clock.hz) if self.chip is not None else 0.0))
        self._update()
        self._plan()

    def _plan(self) -> None:
        chip = self.chip
        op = self.op
        if chip is None:
            return
        if self._ev is not None:
            chip.sched.cancel(self._ev)
            self._ev = None
        if op is None or op.done or op.fmode == 2:
            if op is not None and op.fmode == 2 and not op.done and op.poll:
                p = op.poll
                cands = []
                if p["k_match"] is not None and not p.get("matched"):
                    cands.append(p["t_first"] + p["k_match"] * p["P"])
                if self.get("CR") & FTIE and not self._ftf():
                    # FTF per status read (12.3.6): one event per poll, only while the FTF interrupt is enabled
                    cands.append(p["t_first"] + (self._poll_k(self._now()) + 1) * p["P"] + 1e9 / float(chip.clock.hz))
                if cands:
                    self._ev = chip.sched.at_ns(max(min(cands), self._now()), self._tick)
            return
        now = self._now()
        tb = op.t_byte
        thr = self._thr()
        cands = []
        want_ftf = bool(self.get("CR") & (FTIE | DMAEN))
        if op.n == 0:
            cands.append(op.t_next)
        elif op.fmode == 1:
            space = FIFO_SIZE - len(self.fifo)
            if op.left and space and not op.stall:
                if want_ftf and len(self.fifo) < thr:
                    cands.append(op.t_next + (min(thr - len(self.fifo), op.left) - 1) * tb)
                if op.left <= space:
                    cands.append(op.t_next + (op.left - 1) * tb)
        else:
            free = FIFO_SIZE - len(self.fifo)
            if self.fifo:
                if want_ftf and free < thr:
                    cands.append(op.t_next + (thr - free - 1) * tb)
                if op.left is not None and op.left <= len(self.fifo):
                    cands.append(op.t_next + (op.left - 1) * tb)
        if cands:
            self._ev = chip.sched.at_ns(max(min(cands), now), self._tick)

    def _update(self) -> None:
        chip = self.chip
        if chip is None:
            return
        sr = self._sr_value()
        cr = self.get("CR")
        on = bool(((sr & TEF) and cr & TEIE) or ((sr & TCF) and cr & TCIE) or ((sr & FTF) and cr & FTIE)
                  or ((sr & SMF) and cr & SMIE) or ((sr & TOF) and cr & TOIE))
        if self._irqn is not None and on != self._line:
            self._line = on
            self.irq(self._irqn, on)
        dreq = bool(sr & FTF and cr & DMAEN and self._fmode() in (0, 1))      # DMA: indirect mode only (12.5.1)
        if dreq != self._dreq or dreq:
            self._dreq = dreq
            dma_request(chip, "QUADSPI", dreq)
