"""STM32F4 SDIO host (RM0090 ch. 31), SD memory card side only (no SDIO-card interrupts / read wait, no CE-ATA).

Registers from the SVD (POWER, CLKCR, ARG, CMD, RESPCMD, RESP1-4, DTIMER, DLEN, DCTRL, DCOUNT, STA, ICR, MASK,
FIFOCNT, FIFO at 0x80-0xFC); behaviour hand-written. A card is attached with `attach_card(card)` (the vendor-free
chipem.devices.sdcard.SDCard: `--sd-card SDIO:...`, board `devices: [{type: sd_card, host: SDIO}]`). Without a
card every command that waits for a response ends in CTIMEOUT, as with an empty socket.

* Clocks: SDIOCLK = PLL48CLK (RCC PLLCFGR PLLQ output; 0 while the PLL is off); SDIO_CK = SDIOCLK / (CLKDIV + 2), or
  SDIOCLK with BYPASS. The card clock runs only with POWER.PWRCTRL = 11 and CLKCR.CLKEN = 1 (PWRSAV: only while the
  bus is busy). A command or data transfer started while the clock is stopped waits for it. All bus timing is in
  SDIO_CK periods (`sched.at_ns`, wall-clock domain).
* CPSM: a CMD write with CPSMEN sends the 48-bit command (CMDACT). WAITRESP 00/10: CMDSENT 8 clocks after the end
  bit (NCC). 01/11: the card answers after NCR = 2 clocks (5 for R2/R3, NID) with 48 / 136 bits; RESPCMD (0x3F for
  R2/R3), RESP1-4 (long: CID/CSD bits 127:1, RESP4 bit 0 = 0) and CMDREND, or CCRCFAIL when the CRC7 does not match
  (always for R3, whose CRC field is all ones; a response of the wrong length also fails the CRC). No response
  within 64 clocks: CTIMEOUT. WAITINT / WAITPEND / CE-ATA bits: one `warn`, ignored.
* DPSM (DCTRL write with DTEN = 1 while idle): DCOUNT := DLEN, FIFOCNT := DLEN in words (rounded up). Receive: Wait_R
  until the card's start bit (DTIMER clocks, else DTIMEOUT), then the block is shifted into the FIFO word by word
  (32 / bus width clocks per word); at the block end (CRC16 + end bit) DBCKEND, or DCRCFAIL when the block CRC is
  bad: card on another bus width, card block length != DBLOCKSIZE, a DAT line not muxed, or an injected error; then
  DATAEND once DCOUNT = 0, else Wait_R for the next block (Nac later). A full FIFO when a word arrives is RXOVERR
  (the DPSM stops) unless HWFC_EN stops the clock. Transmit: Wait_S until the FIFO has data and DAT0 is not busy,
  then words leave the FIFO at the bus rate (an empty FIFO mid-block is TXUNDERR, or a clock stop with HWFC_EN);
  after CRC16 + end bit + the card's CRC status token: negative -> DCRCFAIL; positive -> Busy while the card programs
  (DTIMER bounds it: DTIMEOUT), then DBCKEND and DATAEND / the next block. No card (or the card not expecting data):
  no CRC status, DTIMEOUT. DTEN written while the DPSM is busy is ignored (one `warn`). Stream mode (DTMODE, MMC
  only) and SDIO read wait (RWSTART/RWSTOP/SDIOEN): one `warn`; an SD card does not answer a stream transfer.
* FIFO: 32 words. TXFIFOHE / RXFIFOHF at 8 free / 8 stored words (RM0090 31.9.11), TXFIFOF/E, RXFIFOF/E, TXDAVL,
  RXDAVL, TXACT, RXACT, CMDACT are live (with CLKCR.HWFC_EN, TXFIFOE is active at <= 2 words and RXFIFOF 2 words
  before full, RM0090 31.9.11; no FIFO flag before the first data transfer: STA resets to 0, RM0090
  31.9.11); static flags clear through ICR. Received words stay readable after DATAEND
  (RXDAVL) until read. A FIFO write outside a transmit transfer or into a full FIFO is dropped (one `warn`).
* IRQ "SDIO" = (STA & MASK) != 0 (level). DMA (DCTRL.DMAEN): request "SDIO" (DMA2 stream 3 or 6, channel 4) while the
  receive FIFO holds data / the transmit FIFO has room and FIFOCNT > 0, one word per request (silicon issues bursts;
  the DMA model moves one item per request). The request for the last word (FIFOCNT = 1) is flagged as the last
  one, so a DMA stream with PFCTRL = 1 (peripheral flow control, RM0090 10.3.15) completes after it.
* Pins (family signals table): SDIO_CK out L, SDIO_CMD out H, SDIO_D0-7 in. The card only sees the bus when SDIO_CK
  and SDIO_CMD are muxed to pins (else commands time out, one `warn`); data needs D0 (and D1-D3 in 4-bit mode). The
  CK pin is not toggled per edge (up to 48 MHz); while the clock runs, one `pin` event with `field="wave"` (edges,
  since_ns, freq_hz, duty 50 %) per 100 ms window / clock change summarises it, as the event log does for dense pins.
  CMD/DAT levels do not follow the bit stream (like UART/SPI frame contents elsewhere).
* RCC: APB2ENR.SDIOEN gate (a gated-off access logs one `warn`), APB2RSTR.SDIORST resets the block.

STM32F1 high density (params `f1: true`, RM0008 ch. 22): the same SDIO IP (register map 22.9.15 identical, FIFO flags
22.9.11 at 8 words), but SDIOCLK = HCLK (22.2 "SDIO adapter clock (SDIOCLK = HCLK)", RM0008 Figure 8), the clock gate is
RCC_AHBENR.SDIOEN (bit 10; stm32f1.rcc GATES "SDIO") and there is no reset bit (no AHBRSTR outside the connectivity
line).  The F1 DMA has no peripheral flow control: DMA2 channel 4 (request "SDIO", RM0008 13.3.7 Table 79) moves the
number of words programmed in CNDTR, one word per request.
"""
from __future__ import annotations

from collections import deque
from typing import Any

from ..base import MMIOBlock, pure_read
from ..registry import register
from ..sdbus import ClockWave, lines_muxed
from ._util import GatedBlock, build_regs, dma_request, fmt_hz, irq_number
from .dma import controllers as dma_controllers

# CLKCR
CLKEN, PWRSAV, BYPASS, NEGEDGE, HWFC_EN = 1 << 8, 1 << 9, 1 << 10, 1 << 13, 1 << 14
# CMD
WAITINT, WAITPEND, CPSMEN, SDIOSUSPEND, ENCMDCOMPL, NIEN, CEATACMD = (1 << 8, 1 << 9, 1 << 10, 1 << 11, 1 << 12,
                                                                     1 << 13, 1 << 14)
# DCTRL
DTEN, DTDIR, DTMODE, DMAEN, RWSTART, RWSTOP, RWMOD, SDIOEN = 1, 2, 4, 8, 1 << 8, 1 << 9, 1 << 10, 1 << 11
# STA
(CCRCFAIL, DCRCFAIL, CTIMEOUT, DTIMEOUT, TXUNDERR, RXOVERR, CMDREND, CMDSENT, DATAEND, STBITERR, DBCKEND, CMDACT,
 TXACT, RXACT, TXFIFOHE, RXFIFOHF, TXFIFOF, RXFIFOF, TXFIFOE, RXFIFOE, TXDAVL, RXDAVL, SDIOIT, CEATAEND) = \
    (1 << i for i in range(24))
STATIC = 0x7FF | SDIOIT | CEATAEND
FIFO_WORDS = 32

IDLE, WAIT_S, SEND, BUSY, WAIT_R, RECV = "idle", "wait_s", "send", "busy", "wait_r", "recv"


@register("stm32.sdio")
class SDIO(GatedBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.gate = params.get("gate", "SDIO")
        ro = {"ro_mask": 0xFFFFFFFF}
        ov = {"POWER": {"on_write": self._clk_write}, "CLKCR": {"on_write": self._clk_write},
              "CMD": {"on_write": self._cmd_write}, "DCTRL": {"on_write": self._dctrl_write},
              "STA": {**ro, "on_read": self._sta_read}, "ICR": {"on_write": self._icr_write},
              "MASK": {"on_write": self._mask_write}, "RESPCMD": ro, "RESP1": ro, "RESP2": ro, "RESP3": ro,
              "RESP4": ro, "DCOUNT": ro, "FIFOCNT": ro}
        try:
            r = build_regs((name, "SDIO"), size, ov, params.get("regs_file"), skip=("FIFO",))
        except KeyError:        # STM32F446.svd describes another IP ("SDMMC" v2) at this address: use the F405 SDIO
            r = build_regs((name, "SDIO"), size, ov, None, skip=("FIFO",))     # table (same SDIO_TypeDef in the HAL)
        for reg in r:           # STM32F103xx.svd spells SDIO_RESP1 "RESPI1" (RM0008 22.9.6: SDIO_RESP1 at 0x14)
            if reg.name == "RESPI1":
                reg.name = "RESP1"
                reg.__init__("RESP1", reg.offset, reg.width, reg._raw[0], 0xFFFFFFFF, reg._raw[2], reg.on_read,
                             reg.on_write, reg.doc, reg.unit_bits)
        super().__init__(name, base, size, r, log=log, params=params)
        if params.get("ceata") is False:
            # STM32F446 (RM0390 29.8.4 / 29.8.11-29.8.13): no CE-ATA bits (CMD 14:12, CEATAEND 23) and no STBITERR
            # (9): reserved, read 0, writes ignored
            for rn, bits in (("CMD", 0x7000), ("ICR", 0x00800200), ("MASK", 0x00800200)):
                if rn in self.regs:
                    self.regs[rn].ro_mask |= bits
        self.card: Any = None
        self.sig = {"CK": f"{name}_CK", "CMD": f"{name}_CMD", **{f"D{i}": f"{name}_D{i}" for i in range(8)}}
        self._irqn: int | None = None
        self._line = False
        self._dma = False
        self._warned: set[str] = set()
        self.commands: deque[tuple[float, int, int, str]] = deque(maxlen=10000)   # (t_ns, index, arg, result)
        self.wave = ClockWave(self, self.sig["CK"])
        self._init_state()

    def _init_state(self) -> None:
        self.fifo: deque[int] = deque()
        self.fifo_dir: str | None = None   # None until the first data transfer: STA resets to 0 (RM0090 31.9.11)
        self.sta = 0                       # static flags
        self.cpsm = IDLE
        self.dpsm = IDLE
        self._cmd_ev = None
        self._dat_ev = None
        self._dto_ev = None
        self._card_ev = None
        self._pending_cmd = False          # command waiting for the clock
        self._pending_dat = False
        self._blk: dict | None = None
        self._blocksize = 512
        self._dcount = 0
        self._fifocnt = 0
        self._ck_hz = 0.0

    # ------------------------------------------------------------------ setup
    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irqn = irq_number(chip, self.name, "SDIO")
        if self._rcc is not None:
            self._rcc.listeners.append(self._clock_tree_changed)

    def attach_card(self, card: Any) -> None:
        """Insert an SD card (chipem.devices.sdcard.SDCard) into the socket."""
        self.card = card

    def reset(self) -> None:
        if self.chip is not None:
            for ev in (getattr(self, n, None) for n in ("_cmd_ev", "_dat_ev", "_dto_ev", "_card_ev")):
                if ev is not None:
                    self.chip.sched.cancel(ev)
        self.wave.reset()
        if self.chip is not None:
            if self._line:
                self.irq(self._irqn, False)
            if self._dma:
                dma_request(self.chip, "SDIO", False)
        self._line = False
        self._dma = False
        self._init_state()
        self._warned.clear()
        super().reset()

    # ------------------------------------------------------------------ helpers
    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=msg)

    def _muxed(self, k: str) -> bool:
        return lines_muxed(self.chip, (self.sig[k],))

    def sdioclk_hz(self) -> float:
        """SDIOCLK = PLL48CLK (0 while the main PLL is off)."""
        r = self._rcc
        if self.params.get("f1") and self.chip is not None:
            return float(self.chip.clock.hz)             # RM0008 22.2: SDIOCLK = HCLK (= core clock)
        if r is None:
            return float(self.params.get("sdioclk_hz", 48e6))
        g = getattr(r, "sdio_kernel_hz", None)           # STM32F446 DCKCFGR2.SDIOSEL / CK48MSEL (stm32.rcc_f446)
        if g is not None:
            return float(g())
        if not r.get("CR") & (1 << 25):                  # PLLRDY
            return 0.0
        return r.pll_q_hz()

    def ck_hz(self) -> float:
        """SDIO_CK frequency now (0: clock stopped)."""
        if self.get("POWER") & 3 != 3:
            return 0.0
        clk = self.get("CLKCR")
        if not clk & CLKEN:
            return 0.0
        base = self.sdioclk_hz()
        return base if clk & BYPASS else base / ((clk & 0xFF) + 2)

    def bus_width(self) -> int:
        return (1, 4, 8, 1)[(self.get("CLKCR") >> 11) & 3]

    def _T(self) -> float:
        return 1e9 / self._ck_hz if self._ck_hz else 0.0

    def _now(self) -> float:
        return self.chip.now_ns()

    def _at(self, t: float, cb, *args):
        return self.chip.sched.at_ns(t, cb, *args)

    def _cancel(self, attr: str) -> None:
        ev = getattr(self, attr)
        if ev is not None:
            self.chip.sched.cancel(ev)
            setattr(self, attr, None)

    # ------------------------------------------------------------------ clock and CK summary
    def _clock_tree_changed(self) -> None:
        self._clk_update()

    def _clk_write(self, blk, reg, old, new):
        self._store(reg, new)
        if reg.name == "CLKCR" and new & (NEGEDGE) and not old & NEGEDGE:
            pass                                          # sampling edge: no effect on a timing-level model
        self._clk_update()
        return None

    def _clk_update(self) -> None:
        if self.chip is None:
            return
        hz = self.ck_hz()
        if hz != self._ck_hz:
            old = self._ck_hz
            self._ck_hz = hz
            if hz:
                if hz > 50e6 * 1.0001:
                    self._warn_once("fast", f"{self.name}: SDIO_CK {fmt_hz(hz)} is above the 50 MHz limit")
                if self.card is not None and hz > 25e6 * 1.0001 and not getattr(self.card, "high_speed", True):
                    self._warn_once("hs", f"{self.name}: SDIO_CK {fmt_hz(hz)} above 25 MHz while the card is not in "
                                    "high-speed mode (CMD6)")
            elif old and self.get("POWER") & 3 == 3 and self.get("CLKCR") & CLKEN and not self.sdioclk_hz():
                self._warn_once("noclk", f"{self.name}: SDIOCLK (PLL48CLK) stopped: the card clock is off")
        self.wave.set(hz, bool(self.get("CLKCR") & PWRSAV))
        if hz:
            if self._pending_cmd:
                self._pending_cmd = False
                self._cmd_start()
            if self._pending_dat:
                self._pending_dat = False
                self._dpsm_start()
        if hz == 0 and self.get("CLKCR") & CLKEN and self.get("POWER") & 3 == 3 and not self.sdioclk_hz():
            self._warn_once("noclk", f"{self.name}: SDIOCLK (PLL48CLK) is not running (main PLL off): no card clock")

    def _ck_active(self, clocks: float) -> None:
        """PWRSAV: the clock runs only while the bus is busy."""
        self.wave.active(clocks)

    # ------------------------------------------------------------------ status / IRQ / DMA
    def status(self) -> int:
        s = self.sta
        if self.cpsm != IDLE or self._pending_cmd:
            s |= CMDACT
        n = len(self.fifo)
        hwfc = 2 if self.get("CLKCR") & HWFC_EN else 0     # RM0090 31.9.11: HW flow control moves TXFIFOE / RXFIFOF
        if self.dpsm in (WAIT_S, SEND, BUSY):
            s |= TXACT
        elif self.dpsm in (WAIT_R, RECV):
            s |= RXACT
        if self.fifo_dir == "tx":
            if s & TXACT and FIFO_WORDS - n >= 8:
                s |= TXFIFOHE
            s |= (TXFIFOF if n >= FIFO_WORDS else 0) | (TXFIFOE if n <= hwfc else 0) | (TXDAVL if n else 0)
        elif self.fifo_dir == "rx":
            if n >= 8:
                s |= RXFIFOHF
            s |= (RXFIFOF if n >= FIFO_WORDS - hwfc else 0) | (RXFIFOE if n == 0 else RXDAVL)
        return s

    def _flag(self, bits: int) -> None:
        self.sta |= bits
        self._update()

    def _update(self) -> None:
        if self.chip is None:
            return
        st = self.status()
        self.set("STA", st)
        self.set("DCOUNT", self._dcount)
        self.set("FIFOCNT", self._fifocnt)
        line = bool(st & self.get("MASK"))
        if line != self._line and self._irqn is not None:
            self._line = line
            self.irq(self._irqn, line)
        want = False
        if self.get("DCTRL") & DMAEN:
            if self.dpsm in (WAIT_R, RECV) or (self.fifo_dir == "rx" and self.fifo):
                want = bool(self.fifo) and self.fifo_dir == "rx"
            elif self.dpsm in (WAIT_S, SEND, BUSY):
                want = len(self.fifo) < FIFO_WORDS and self._fifocnt > 0
        if want:
            # re-armed after every acknowledged item; the request that moves the last word (FIFOCNT = 1) carries
            # the "last data" signal for a DMA stream with PFCTRL (RM0090 10.3.15)
            last = self._fifocnt == 1
            for c in dma_controllers(self.chip):
                c.request("SDIO", True, last=last)
            self._dma = True
        elif self._dma:
            dma_request(self.chip, "SDIO", False)
            self._dma = False

    @pure_read
    def _sta_read(self, blk, reg):
        return self.status()

    def _icr_write(self, blk, reg, old, new):
        self.sta &= ~(new & STATIC)
        self._update()
        return 0

    def _mask_write(self, blk, reg, old, new):
        self._store(reg, new)
        self._update()
        return None

    # ------------------------------------------------------------------ FIFO access (0x80-0xFF)
    def read(self, offset: int, size: int) -> int:
        if self._gate_loc is not None:
            self._gate_ok()
        if 0x80 <= offset < 0x100:
            v = self._fifo_read()
            if size < 4:
                v = (v >> (8 * (offset & 3))) & ((1 << (8 * size)) - 1)
            return v
        return MMIOBlock.read(self, offset, size)

    def write(self, offset: int, size: int, value: int) -> None:
        if self._gate_loc is not None:
            self._gate_ok()
        if 0x80 <= offset < 0x100:
            self._fifo_write(value & 0xFFFFFFFF)
            return
        MMIOBlock.write(self, offset, size, value)

    def _fifo_read(self) -> int:
        if self.fifo_dir != "rx" or not self.fifo:
            return 0
        v = self.fifo.popleft()
        if self._fifocnt:
            self._fifocnt -= 1
        self._update()
        return v

    def _fifo_write(self, v: int) -> None:
        if self.dpsm not in (WAIT_S, SEND, BUSY):
            self._warn_once("fifo_idle", f"{self.name}: FIFO write while no transmit transfer is active: dropped")
            return
        if len(self.fifo) >= FIFO_WORDS:
            self._warn_once("fifo_full", f"{self.name}: FIFO write while the transmit FIFO is full: dropped")
            return
        self.fifo.append(v)
        if self._fifocnt:
            self._fifocnt -= 1
        if self.dpsm == WAIT_S:
            self._try_send()
        self._update()

    # ------------------------------------------------------------------ command path
    def _cmd_write(self, blk, reg, old, new):
        self._store(reg, new)
        if not new & CPSMEN:
            return None
        if self.cpsm != IDLE or self._pending_cmd:
            self._warn_once("cmd_busy", f"{self.name}: CMD written while the CPSM is busy (CMDACT): ignored")
            return None
        if new & (WAITINT | WAITPEND | SDIOSUSPEND | CEATACMD | ENCMDCOMPL):
            self._warn_once("cmd_bits", f"{self.name}: CMD WAITINT/WAITPEND/SDIOSuspend/CE-ATA bits are not "
                            "modelled (ignored)")
        if self._ck_hz:
            self._cmd_start()
        else:
            self._pending_cmd = True
            self._update()
        return None

    def _cmd_start(self) -> None:
        cmd = self.get("CMD")
        arg = self.get("ARG")
        T = self._T()
        self.cpsm = "send"
        self._ck_active(48)
        self._cmd_ev = self._at(self._now() + 48 * T, self._cmd_at_card, cmd & 0x3F, arg, (cmd >> 6) & 3)
        self._update()

    def _cmd_at_card(self, index: int, arg: int, waitresp: int) -> None:
        self._cmd_ev = None
        t = self._now()
        T = self._T() or 1.0
        resp = None
        wired = self._muxed("CK") and self._muxed("CMD")
        if self.card is not None:
            if wired:
                resp = self.card.command(index, arg, t)
            else:
                self._warn_once("nowire", f"{self.name}: SDIO_CK / SDIO_CMD not muxed to a pin: the card does not "
                                "see the command")
        if resp is not None and resp.data == "read":
            nac = max(self.card.read_ready_ns, 2 * T)
            ncr = 5 if resp.nbits == 136 or resp.kind == "R3" else 2
            self._schedule_card_block(t + (ncr + resp.nbits) * T + nac)
        if waitresp in (0, 2):
            self.commands.append((t, index, arg, "sent"))
            self.cpsm = "ncc"
            self._ck_active(8)
            self._cmd_ev = self._at(t + 8 * T, self._cmd_done, CMDSENT, None, False)
            return
        long = waitresp == 3
        if resp is None:
            self.commands.append((t, index, arg, "timeout"))
            self.cpsm = "wait"
            self._ck_active(64)
            self._cmd_ev = self._at(t + 64 * T, self._cmd_done, CTIMEOUT, None, long)
            return
        ncr = 5 if resp.nbits == 136 or resp.kind == "R3" else 2
        self.cpsm = "wait"
        self._ck_active(ncr + resp.nbits)
        self.commands.append((t, index, arg, resp.kind))
        self._cmd_ev = self._at(t + (ncr + resp.nbits) * T, self._cmd_done, None, resp, long)

    def _cmd_done(self, flag: int | None, resp: Any, long: bool) -> None:
        self._cmd_ev = None
        self.cpsm = IDLE
        if resp is not None:
            ok = resp.crc_ok()
            if long:
                if resp.nbits == 136:
                    reg = resp.payload
                    self.set("RESPCMD", 0x3F)
                else:
                    reg = resp.frame << 88                     # a 48-bit token where 136 bits were expected
                    self.set("RESPCMD", resp.index)
                    ok = False
                self.set("RESP1", (reg >> 96) & 0xFFFFFFFF)
                self.set("RESP2", (reg >> 64) & 0xFFFFFFFF)
                self.set("RESP3", (reg >> 32) & 0xFFFFFFFF)
                self.set("RESP4", reg & 0xFFFFFFFE)
            else:
                if resp.nbits == 136:
                    frame48 = resp.frame >> 88                 # first 48 bits of a long token
                    self.set("RESPCMD", (frame48 >> 40) & 0x3F)
                    self.set("RESP1", (frame48 >> 8) & 0xFFFFFFFF)
                    ok = False
                else:
                    self.set("RESPCMD", resp.index)
                    self.set("RESP1", resp.payload)
            flag = CMDREND if ok else CCRCFAIL
        self._flag(flag)

    # ------------------------------------------------------------------ data path
    def _dctrl_write(self, blk, reg, old, new):
        self._store(reg, new)
        if new & (RWSTART | RWSTOP | SDIOEN):
            self._warn_once("rw", f"{self.name}: SDIO read wait / SDIO-card mode (RWSTART/RWSTOP/SDIOEN) is not "
                            "modelled")
        if new & DTEN:
            if self.dpsm != IDLE or self._pending_dat:
                self._warn_once("dpsm_busy", f"{self.name}: DCTRL.DTEN written while a data transfer is active: "
                                "ignored")
            elif self._ck_hz:
                self._dpsm_start()
            else:
                self._pending_dat = True
        self._update()
        return None

    def _dpsm_start(self) -> None:
        dctrl = self.get("DCTRL")
        dlen = self.get("DLEN") & 0x1FFFFFF
        self._dcount = dlen
        self._fifocnt = (dlen + 3) // 4
        bs = (dctrl >> 4) & 0xF
        if bs == 15:
            self._warn_once("bs", f"{self.name}: DBLOCKSIZE = 15 is reserved")
        self._blocksize = 1 << min(bs, 14)
        if dctrl & DTMODE:
            self._warn_once("stream", f"{self.name}: stream mode (DTMODE = 1) is for MultiMediaCards; an SD card does "
                            "not answer it")
        elif dlen % self._blocksize:
            self._warn_once("dlen", f"{self.name}: DLEN {dlen} is not a multiple of the block size "
                            f"{self._blocksize}")
        if dctrl & DTDIR:
            self.fifo_dir = "rx"
            self.dpsm = WAIT_R
            self._arm_timeout()
        else:
            if self.fifo_dir != "tx":
                self.fifo.clear()
            self.fifo_dir = "tx"
            self.dpsm = WAIT_S
            self._try_send()
        self._update()

    def _arm_timeout(self) -> None:
        self._cancel("_dto_ev")
        T = self._T()
        if T:
            self._dto_ev = self._at(self._now() + self.get("DTIMER") * T, self._dtimeout)

    def _dtimeout(self) -> None:
        self._dto_ev = None
        if self.dpsm in (WAIT_R, BUSY, WAIT_S):
            self._cancel("_dat_ev")
            self.dpsm = IDLE
            self._flag(DTIMEOUT)

    def _data_lines_ok(self) -> bool:
        need = ("D0",) if self.bus_width() == 1 else ("D0", "D1", "D2", "D3") if self.bus_width() == 4 else \
            tuple(f"D{i}" for i in range(8))
        ok = all(self._muxed(k) for k in need)
        if not ok:
            self._warn_once("nodat", f"{self.name}: data line(s) {', '.join(need)} not all muxed to pins: blocks "
                            "arrive corrupted")
        return ok

    # ---- receive
    def _schedule_card_block(self, t: float) -> None:
        if self._card_ev is not None:
            self.chip.sched.cancel(self._card_ev)
        self._card_ev = self._at(t, self._rx_block_start)

    def _rx_block_start(self) -> None:
        self._card_ev = None
        card = self.card
        if card is None or not self._ck_hz:
            return
        width = self.bus_width()
        t = self._now()
        data = card.read_block(t, width)
        if data is None:
            return
        T = self._T()
        bad = card.take_crc_error() if hasattr(card, "take_crc_error") else False
        nbits = len(data) * 8 / width
        self._ck_active(nbits + 18)
        if self.dpsm != WAIT_R or self.get("DCTRL") & DTMODE:
            # nobody listens: the card still sends this block; the next follows Nac later
            self._schedule_card_block(t + (nbits + 18) * T + max(card.read_ready_ns, 2 * T))
            return
        self._cancel("_dto_ev")
        expect = min(self._blocksize, self._dcount) if self._dcount else self._blocksize
        bad = bad or card.bus_width != width or len(data) != expect or not self._data_lines_ok()
        buf = bytes(data[:expect]).ljust(expect, b"\xff")
        self.dpsm = RECV
        self._blk = {"buf": buf, "pos": 0, "bad": bad, "width": width}
        self._dat_ev = self._at(t + T + min(32, expect * 8) / width * T, self._rx_word)
        self._update()

    def _rx_word(self) -> None:
        self._dat_ev = None
        b = self._blk
        if b is None or self.dpsm != RECV:
            return
        T = self._T()
        if not T:
            return
        buf, pos = b["buf"], b["pos"]
        if len(self.fifo) >= FIFO_WORDS:
            if self.get("CLKCR") & HWFC_EN:
                self._dat_ev = self._at(self._now() + 8 * T, self._rx_word)     # clock stopped until room
                return
            self.dpsm = IDLE
            self._blk = None
            self._flag(RXOVERR)
            return
        n = min(4, len(buf) - pos)
        self.fifo.append(int.from_bytes(buf[pos:pos + n], "little"))
        self._dcount = max(0, self._dcount - n)
        pos += n
        b["pos"] = pos
        if pos < len(buf):
            k = min(4, len(buf) - pos)
            self._dat_ev = self._at(self._now() + k * 8 / b["width"] * T, self._rx_word)
        else:
            self._dat_ev = self._at(self._now() + 17 * T, self._rx_block_end)
        self._update()

    def _rx_block_end(self) -> None:
        self._dat_ev = None
        b = self._blk
        self._blk = None
        if b is None:
            return
        if b["bad"]:
            self.dpsm = IDLE
            self._flag(DCRCFAIL)
            return
        flags = DBCKEND
        if self._dcount == 0:
            self.dpsm = IDLE
            flags |= DATAEND
        else:
            self.dpsm = WAIT_R
            self._arm_timeout()
        if self.card is not None:
            T = self._T() or 1.0
            self._schedule_card_block(self._now() + max(self.card.read_ready_ns, 2 * T))
        self._flag(flags)

    # ---- transmit
    def _try_send(self) -> None:
        if self.dpsm != WAIT_S or not self._ck_hz or self._dat_ev is not None:
            return
        if self._dcount == 0:
            return
        if not self.fifo:
            return
        card = self.card
        t = self._now()
        if card is not None and card.busy(t):
            self._dat_ev = self._at(card.busy_until, self._resend)
            return
        T = self._T()
        expect = min(self._blocksize, self._dcount)
        self.dpsm = SEND
        self._cancel("_dto_ev")
        self._blk = {"buf": bytearray(), "expect": expect, "width": self.bus_width()}
        self._dat_ev = self._at(t + 3 * T + min(32, expect * 8) / self.bus_width() * T, self._tx_word)

    def _resend(self) -> None:
        self._dat_ev = None
        self._try_send()

    def _tx_word(self) -> None:
        self._dat_ev = None
        b = self._blk
        if b is None or self.dpsm != SEND:
            return
        T = self._T()
        if not T:
            return
        if not self.fifo:
            if self.get("CLKCR") & HWFC_EN:
                self._dat_ev = self._at(self._now() + 8 * T, self._tx_word)
                return
            self.dpsm = IDLE
            self._blk = None
            self._flag(TXUNDERR)
            return
        w = self.fifo.popleft()
        n = min(4, b["expect"] - len(b["buf"]))
        b["buf"] += w.to_bytes(4, "little")[:n]
        self._dcount = max(0, self._dcount - n)
        self._ck_active(n * 8 / b["width"])
        if len(b["buf"]) < b["expect"]:
            k = min(4, b["expect"] - len(b["buf"]))
            self._dat_ev = self._at(self._now() + k * 8 / b["width"] * T, self._tx_word)
        else:
            self._ck_active(24)
            self._dat_ev = self._at(self._now() + (17 + 2 + 5) * T, self._tx_block_end)
        self._update()

    def _tx_block_end(self) -> None:
        self._dat_ev = None
        b = self._blk
        self._blk = None
        if b is None:
            return
        card = self.card
        t = self._now()
        if card is None or not card.write_expected():
            self.dpsm = BUSY                                # no CRC status token: waits for DTIMER
            self._arm_timeout()
            self._update()
            return
        crc_ok = (b["expect"] == 512 and not self.get("DCTRL") & DTMODE and self._data_lines_ok())
        ok, busy = card.write_block(bytes(b["buf"]), t, b["width"], crc_ok=crc_ok)
        if not ok:
            self.dpsm = IDLE
            self._flag(DCRCFAIL)
            return
        self.dpsm = BUSY
        self._arm_timeout()
        self._dat_ev = self._at(t + busy, self._tx_busy_end)
        self._update()

    def _tx_busy_end(self) -> None:
        self._dat_ev = None
        if self.dpsm != BUSY:
            return
        self._cancel("_dto_ev")
        flags = DBCKEND
        if self._dcount == 0:
            self.dpsm = IDLE
            flags |= DATAEND
        else:
            self.dpsm = WAIT_S
        self._flag(flags)
        self._try_send()
        self._update()
