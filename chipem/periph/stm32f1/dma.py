"""STM32F1 DMA1 (RM0008 ch. 13 layout, low/medium-density 7-channel controller).

Not verified against RM0008 (the reference manual was not available when this was written): behaviour from the
RM0008 DMA chapter as known and as used by the HAL/LL/SPL drivers; register layout from ST's STM32F103xx.svd
(chips/stm32f103_regs.yaml: ISR, IFCR, CCRn/CNDTRn/CPARn/CMARn, n = 1..7, channel stride 0x14).

Request API (same as stm32.dma, so the reused stm32.usart / spi / i2c / tim models and stm32f1.adc reach it through
`chipem.periph.stm32._util.dma_request(chip, name, level)`; the block declares `stm32_dma_api = True`):

    DMA.request(name, level=True, last=False) -> bool      DMA.request_active(name) -> bool

`name` is a peripheral request ("USART1_TX", "SPI1_RX", "TIM2_CH3", "TIM1_UP", "ADC1" ...).  The fixed mapping of
RM0008 Table 78 (DMA1 requests per channel) is `REQUESTS`; a channel's request line is the OR of the requests mapped
to it (each peripheral gates its own request with its DMA enable bit, e.g. USART CR3.DMAT, ADC CR2.DMA).  The level /
acknowledge semantics are those of stm32.dma: a request stays asserted until the peripheral deasserts it or the
channel serving it moves ONE data item (acknowledge); a request asserted while no channel is enabled is remembered.
"MEM2MEM" is accepted as a no-op name.  `last` is accepted for API compatibility (no peripheral flow control on the
F1).  When CNDTR reaches 0 the controller calls `dma_transfer_complete(names)` on every peripheral that has it (names =
the requests mapped to the channel), as stm32.dma does.

Transfers take zero bus time but never run inside the MMIO access that triggered them: a request or the EN write
schedules a service event at the current cycle (`sched.in_cycles(0)`).  One service pass arbitrates like RM0008 13.3.2:
among the channels with a pending request (or MEM2MEM) the highest CCR.PL wins, the lower channel number on a tie;
each grant moves one item, so a memory-to-memory channel (always ready) finishes before a lower-priority channel moves.

Per channel (CCRx: EN TCIE HTIE TEIE DIR CIRC PINC MINC PSIZE MSIZE PL MEM2MEM):
* DIR = 0: read from CPAR ("peripheral"), write to CMAR; DIR = 1: read CMAR, write CPAR.  MEM2MEM = 1: the channel runs
  without a request as soon as EN is set (direction still from DIR); MEM2MEM with CIRC is not allowed (one `warn`, a
  single pass is run).
* Data sizes (RM0008 Table 79): the source is read with its own width (PSIZE for the peripheral side, MSIZE for the
  memory side) and the destination written with its width: a narrower source is zero-extended (B0 -> 0x00B0,
  0x000000B0), a wider one truncated to its low bytes (B3B2B1B0 -> B1B0 / B0).  PINC/MINC step each pointer by its own
  size.  PSIZE/MSIZE = 11 (reserved) refuses the enable (one `warn`).  The AHB-to-APB byte-lane replication of
  RM0008 13.3.4 (a byte write to a 32-bit APB register written as B0B0B0B0) is not modelled.
* CNDTR (16 bits) counts items down.  It is read-only while EN = 1 (and so are CPAR and CMAR: writes ignored); a
  channel with CNDTR = 0 transfers nothing even when enabled.  At CNDTR = 0: TCIF; in circular mode CNDTR is reloaded
  with the programmed value and the internal pointers restart from CPAR/CMAR; otherwise the channel stops with EN still
  set (the F1 does not clear EN on completion: software must clear it before reprogramming CNDTR).  HTIF when CNDTR
  reaches half of the programmed count (CNDTR == N // 2).  CPAR/CMAR are never modified (internal pointers, loaded
  when EN is set).
* CCRx configuration bits (all but EN and the TCIE/HTIE/TEIE enables) are kept while EN = 1 (one `warn`): the RM asks
  software to configure the channel with EN = 0.  Clearing EN stops the channel at once (no flag set); setting it again
  restarts the pointers from CPAR/CMAR with the remaining CNDTR.
* ISR: GIFx / TCIFx / HTIFx / TEIFx (bits 4(x-1) .. 4(x-1)+3), read-only; GIFx is set with any of the three.  IFCR is
  write-1-to-clear (reads 0): CTCIFx/CHTIFx/CTEIFx clear one flag, CGIFx clears all four flags of the channel.
* Channel interrupt "DMA1_Channelx" (number from the chip YAML): level of (TCIF & TCIE) | (HTIF & HTIE) |
  (TEIF & TEIE).
* Transfer error: an access the bus cannot resolve (BusError: unmapped address outside the auto-stub windows) sets
  TEIF and GIF and clears EN (RM0008: the faulty channel is disabled by hardware).
Clock gate RCC_AHBENR.DMA1EN: a register access with the clock off logs one `warn` (transfers still run, like the rest
of the family).  There is no AHB reset register on this line: only a chip reset clears the controller.
"""
from __future__ import annotations

from typing import Any

from ...bus import BusError
from ..base import MMIOBlock
from ..registry import register
from ._util import GatedBlock, build_regs, irq_number

NCH = 7

# RM0008 Table 78 "Summary of each channel's requests" (DMA1), channel 1..7.  TIM4 / USART3 / SPI2 / I2C2 requests are
# listed although the low-density F103T6 has none of those peripherals (they exist on medium/high density parts).
REQUESTS: dict[int, tuple[str, ...]] = {
    1: ("ADC1", "TIM2_CH3", "TIM4_CH1"),
    2: ("SPI1_RX", "USART3_TX", "TIM1_CH1", "TIM2_UP", "TIM3_CH3"),
    3: ("SPI1_TX", "USART3_RX", "TIM1_CH2", "TIM3_CH4", "TIM3_UP"),
    4: ("SPI2_RX", "I2S2_RX", "USART1_TX", "I2C2_TX", "TIM1_CH4", "TIM1_TRIG", "TIM1_COM", "TIM4_CH2"),
    5: ("SPI2_TX", "I2S2_TX", "USART1_RX", "I2C2_RX", "TIM1_UP", "TIM2_CH1", "TIM4_CH3"),
    6: ("USART2_RX", "I2C1_TX", "TIM1_CH3", "TIM3_CH1", "TIM3_TRIG"),
    7: ("USART2_TX", "I2C1_RX", "TIM2_CH2", "TIM2_CH4", "TIM4_UP"),
}
CHANNEL_OF = {n: ch for ch, names in REQUESTS.items() for n in names}

# CCR
EN, TCIE, HTIE, TEIE, DIR, CIRC, PINC, MINC = 1, 2, 4, 8, 16, 32, 64, 128
MEM2MEM = 1 << 14
IE_BITS = TCIE | HTIE | TEIE
CR_FREE = EN | IE_BITS                     # writable while the channel is enabled
# ISR flag bits relative to the channel's nibble
F_G, F_TC, F_HT, F_TE = 1, 2, 4, 8
_SIZES = (1, 2, 4, 0)                      # PSIZE/MSIZE 11 = reserved


class _Chan:
    __slots__ = ("n", "irq", "line", "n0", "pptr", "mptr", "psize", "msize", "pstep", "mstep", "circ", "m2m",
                 "dir", "warned_cfg")

    def __init__(self, n: int):
        self.n = n
        self.irq: int | None = None
        self.line = False
        self.clear()

    def clear(self) -> None:
        self.n0 = 0                         # programmed CNDTR (reload value)
        self.pptr = self.mptr = 0
        self.psize = self.msize = 1
        self.pstep = self.mstep = 0
        self.circ = self.m2m = False
        self.dir = 0
        self.warned_cfg = False


@register("stm32f1.dma")
class DMA(GatedBlock):
    model = "stm32f1.dma"
    stm32_dma_api = True                    # found by chipem.periph.stm32.dma.controllers()

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.gate = str(params.get("gate", name))
        ov: dict[str, dict] = {
            "ISR": {"ro_mask": 0xFFFFFFFF},
            "IFCR": {"on_write": self._ifcr_write, "on_read": lambda b, r: 0},
        }
        for c in range(1, NCH + 1):
            ov[f"CCR{c}"] = {"on_write": self._ccr_write(c), "ro_mask": 0xFFFF8000}
            ov[f"CNDTR{c}"] = {"on_write": self._cndtr_write(c), "ro_mask": 0xFFFF0000}
            ov[f"CPAR{c}"] = {"on_write": self._prot_write(c)}
            ov[f"CMAR{c}"] = {"on_write": self._prot_write(c)}
        r = build_regs((name, "DMA1"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.known = set(CHANNEL_OF)
        self.levels: dict[str, bool] = {}
        self.chans = [None] + [_Chan(c) for c in range(1, NCH + 1)]
        self._ccr = [None] + [self.regs[f"CCR{c}"] for c in range(1, NCH + 1)]
        self._cndtr = [None] + [self.regs[f"CNDTR{c}"] for c in range(1, NCH + 1)]
        self._ev = None
        self._busy = False
        self.items = 0                      # data items moved (statistics)

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        for c in range(1, NCH + 1):
            self.chans[c].irq = irq_number(chip, f"{self.name}_Channel{c}", f"DMA1_Channel{c}")

    def reset(self) -> None:
        if self._ev is not None and self.chip is not None:
            self.chip.sched.cancel(self._ev)
        self._ev = None
        self._busy = False
        for ch in self.chans[1:]:
            if ch.line and ch.irq is not None and self.chip is not None:
                self.irq(ch.irq, False)
            ch.line = False
            ch.clear()
        super().reset()

    def write(self, offset: int, size: int, value: int) -> None:
        if self._gate_loc is not None:
            self._gate_ok()
        MMIOBlock.write(self, offset, size, value)

    # ------------------------------------------------------------------ request API (stm32.dma compatible)
    def request(self, name: str, level: bool = True, last: bool = False) -> bool:
        """Assert (level=True) or deassert peripheral DMA request `name` (RM0008 Table 78).  False when no channel of
        this controller serves it."""
        c = CHANNEL_OF.get(name)
        if c is None:
            return name == "MEM2MEM"
        self.levels[name] = bool(level)
        if level:
            ccr = self._ccrv(c)
            if ccr & EN and not ccr & MEM2MEM:
                self._kick()
        return True

    def request_active(self, name: str) -> bool:
        return bool(self.levels.get(name))

    def channel_requests(self, c: int) -> tuple[str, ...]:
        return REQUESTS[c]

    # ------------------------------------------------------------------ helpers
    def _ccrv(self, c: int) -> int:
        r = self._ccr[c]
        return int.from_bytes(self.mem[r.boff:r.boff + 4], "little")

    def _ndt(self, c: int) -> int:
        r = self._cndtr[c]
        return int.from_bytes(self.mem[r.boff:r.boff + 2], "little")

    def flags(self, c: int) -> int:
        """Channel c flags as F_G | F_TC | F_HT | F_TE (0..15)."""
        return (self.get("ISR") >> (4 * (c - 1))) & 0xF

    def _set_flags(self, c: int, bits: int) -> None:
        self.set("ISR", self.get("ISR") | ((bits | F_G) << (4 * (c - 1))))
        self._update_line(c)

    def _update_line(self, c: int) -> None:
        ch = self.chans[c]
        f = self.flags(c)
        ccr = self._ccrv(c)
        lvl = bool((f & F_TC and ccr & TCIE) or (f & F_HT and ccr & HTIE) or (f & F_TE and ccr & TEIE))
        if lvl != ch.line:
            ch.line = lvl
            if ch.irq is not None and self.chip is not None:
                self.irq(ch.irq, lvl)

    lp_frozen: Any = False                  # PWR freezer: Stop / gated Sleep (no HCLK)

    def time_shift(self, dns: float, dcycles: int) -> None:
        """Clock back: requests asserted while the controller had no clock are served now."""
        self._kick()

    def _kick(self) -> None:
        if self.lp_frozen:
            return
        if self._ev is None and not self._busy and self.chip is not None:
            self._ev = self.chip.sched.in_cycles(0, self._service)

    # ------------------------------------------------------------------ register hooks
    def _ifcr_write(self, blk, reg, old, new):
        isr = self.get("ISR")
        for c in range(1, NCH + 1):
            nib = (new >> (4 * (c - 1))) & 0xF
            if nib & F_G:
                nib = 0xF                    # CGIFx clears GIF, TCIF, HTIF and TEIF of the channel
            isr &= ~(nib << (4 * (c - 1)))
        self.set("ISR", isr)
        for c in range(1, NCH + 1):
            self._update_line(c)
        return 0

    def _prot_write(self, c: int):
        def wr(blk, reg, old, new):
            if self._ccrv(c) & EN:
                return old                   # must not be written while the channel is enabled: ignored
            return None
        return wr

    def _cndtr_write(self, c: int):
        def wr(blk, reg, old, new):
            if self._ccrv(c) & EN:
                return old                   # read-only while the channel is enabled
            self.chans[c].n0 = new & 0xFFFF
            return None
        return wr

    def _ccr_write(self, c: int):
        def wr(blk, reg, old, new):
            ch = self.chans[c]
            if old & EN:
                if (old ^ new) & ~CR_FREE & 0x7FFF and not ch.warned_cfg:
                    ch.warned_cfg = True
                    self.emit("warn", block=self.name, msg=f"{self.name} channel {c}: CCR configuration written while "
                              "the channel is enabled (EN = 1): ignored, configure the channel with EN = 0")
                new = (old & ~CR_FREE) | (new & CR_FREE)
                self._store(reg, new)
                self._update_line(c)
                return new
            self._store(reg, new)
            if new & EN:
                self._enable(c)
            self._update_line(c)
            return None
        return wr

    def _enable(self, c: int) -> None:
        ch = self.chans[c]
        ccr = self._ccrv(c)
        psize, msize = _SIZES[ccr >> 8 & 3], _SIZES[ccr >> 10 & 3]
        if not psize or not msize:
            self.emit("warn", block=self.name, msg=f"{self.name} channel {c}: PSIZE/MSIZE = 11 is reserved; channel "
                      "not enabled")
            self._store(self._ccr[c], ccr & ~EN)
            return
        ch.m2m = bool(ccr & MEM2MEM)
        ch.circ = bool(ccr & CIRC)
        if ch.m2m and ch.circ:
            self.emit("warn", block=self.name, msg=f"{self.name} channel {c}: memory-to-memory mode cannot be used "
                      "with circular mode (RM0008); running a single pass")
            ch.circ = False
        ch.dir = 1 if ccr & DIR else 0
        ch.psize, ch.msize = psize, msize
        ch.pstep = psize if ccr & PINC else 0
        ch.mstep = msize if ccr & MINC else 0
        ch.pptr = self.get(f"CPAR{c}")
        ch.mptr = self.get(f"CMAR{c}")
        if self._ndt(c) and (ch.m2m or any(self.levels.get(n) for n in REQUESTS[c])):
            self._kick()

    # ------------------------------------------------------------------ bus access
    def _rd(self, addr: int, n: int) -> int:
        try:
            return self.chip.bus.read(addr, n)
        except BusError as e:
            raise _DmaFault(addr, str(e)) from None

    def _wr(self, addr: int, n: int, v: int) -> None:
        try:
            self.chip.bus.write(addr, n, v)
        except BusError as e:
            raise _DmaFault(addr, str(e)) from None

    # ------------------------------------------------------------------ transfer engine
    def _ready(self, c: int, ccr: int) -> bool:
        if not ccr & EN or not self._ndt(c):
            return False
        if ccr & MEM2MEM:
            return True
        levels = self.levels
        for n in REQUESTS[c]:
            if levels.get(n):
                return True
        return False

    def _service(self) -> None:
        self._ev = None
        self._busy = True
        try:
            guard = 0
            while guard < 2_000_000:
                best, best_pl = 0, -1
                for c in range(1, NCH + 1):
                    ccr = self._ccrv(c)
                    if ccr & EN and (ccr >> 12 & 3) > best_pl and self._ready(c, ccr):
                        best, best_pl = c, ccr >> 12 & 3
                if not best:
                    break
                guard += 1
                self._item(best)
        finally:
            self._busy = False

    def _item(self, c: int) -> None:
        """Move one data item on channel c (one granted request)."""
        ch = self.chans[c]
        if not ch.m2m:
            for n in REQUESTS[c]:            # acknowledge before the access: a re-request during it is kept
                self.levels[n] = False
        try:
            if ch.dir:                       # memory -> peripheral
                v = self._rd(ch.mptr, ch.msize)
                self._wr(ch.pptr, ch.psize, v & ((1 << (8 * ch.psize)) - 1))
            else:                            # peripheral (or M2M source) -> memory
                v = self._rd(ch.pptr, ch.psize)
                self._wr(ch.mptr, ch.msize, v & ((1 << (8 * ch.msize)) - 1))
        except _DmaFault as e:
            self.emit("warn", block=self.name, msg=f"{self.name} channel {c}: bus error at 0x{e.addr:08x} ({e.msg}); "
                      "TEIF set, channel disabled")
            self._store(self._ccr[c], self._ccrv(c) & ~EN)
            self._set_flags(c, F_TE)
            return
        ch.pptr = (ch.pptr + ch.pstep) & 0xFFFFFFFF
        ch.mptr = (ch.mptr + ch.mstep) & 0xFFFFFFFF
        self.items += 1
        ndt = self._ndt(c) - 1
        self.set(f"CNDTR{c}", ndt)
        flags = 0
        if ndt == ch.n0 // 2 and ch.n0 > 1:
            flags |= F_HT
        if ndt == 0:
            flags |= F_TC
            if ch.circ:
                self.set(f"CNDTR{c}", ch.n0)
                ch.pptr = self.get(f"CPAR{c}")
                ch.mptr = self.get(f"CMAR{c}")
            if not ch.m2m:
                self._notify_last(REQUESTS[c])
        if flags:
            self._set_flags(c, flags)

    def _notify_last(self, names: tuple[str, ...]) -> None:
        for blk in (getattr(self.chip, "peripherals", {}) or {}).values():
            fn = getattr(blk, "dma_transfer_complete", None)
            if fn is not None:
                fn(names)


class _DmaFault(Exception):
    def __init__(self, addr: int, msg: str):
        super().__init__(msg)
        self.addr = addr
        self.msg = msg
