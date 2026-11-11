"""STM32F1 DMA controllers (RM0008 ch. 13): DMA1 (7 channels) and DMA2 (5 channels, high-density, XL-density and
connectivity line).

Verified against RM0008 Rev 21 ch. 13 (13.3 functional description, 13.4 registers, Tables 76-80); register layout
from ST's STM32F103xx.svd (chips/stm32f103_regs.yaml: ISR, IFCR, CCRn/CNDTRn/CPARn/CMARn, channel stride 0x14 = 0d20).

Parameters: `controller` (1 or 2; default from the block name), `channels` (default 7 for DMA1, 5 for DMA2),
`apb_byte_lanes` (default true), `apb_ranges` (default APB1 + APB2, 0x4000_0000-0x4001_7FFF).

Request API (same as stm32.dma, so the reused stm32.usart / spi / i2c / tim / dac / sdio models and stm32f1.adc reach it
through `chipem.periph.stm32._util.dma_request(chip, name, level)`; the block declares `stm32_dma_api = True`):

    DMA.request(name, level=True, last=False) -> bool      DMA.request_active(name) -> bool

`name` is a peripheral request ("USART1_TX", "SPI1_RX", "TIM2_CH3", "TIM1_UP", "ADC1", "SPI3_RX", "ADC3", "DAC1" ...).
The fixed mappings are 13.3.7 Table 78 (DMA1, `REQUESTS`) and Table 79 (DMA2, `REQUESTS2`); a channel's request line is
the OR of the requests mapped to it (13.3.7: "simply logically ORed"; each peripheral gates its own request with its
DMA enable bit).  The level / acknowledge semantics are those of stm32.dma (13.3.1): a request stays asserted until the
peripheral deasserts it or the channel serving it moves ONE data item; a request asserted while no channel is enabled
is remembered.  "MEM2MEM" is accepted as a no-op name; `last` is accepted for API compatibility.  When CNDTR reaches 0
the controller calls `dma_transfer_complete(names)` on every peripheral that has it, as stm32.dma does.

Verified (RM0008 section):
* Arbitration (13.3.2): among the channels with a pending request (or MEM2MEM) the highest CCR.PL wins, the lower channel
  number on a tie; each grant moves one item.  DMA1 is served before DMA2 when both have work at the same instant
  (13.3.2 note).
* Channels (13.3.3): DIR = 0 reads CPAR and writes CMAR, DIR = 1 the reverse; MEM2MEM runs without a request as soon as
  EN is set and stops at CNDTR = 0; MEM2MEM with CIRC "may not be used" (one `warn`, a single pass is run).  Circular
  mode reloads CNDTR and the internal pointers from CPAR/CMAR.  CPAR/CMAR keep their programmed value (the current
  pointers are internal).  A disabled channel keeps its registers.  Without CIRC the channel serves no request after
  CNDTR = 0 and EN stays set (software clears it to reload CNDTR).
* Data widths (13.3.4 Table 76): the source is read with its own width and the destination written with its width; a
  narrower source is zero-extended (B0 -> 00B0 / 000000B0), a wider one truncated to its low bytes (B3B2B1B0 -> B1B0 / B0);
  PINC/MINC step by the own size.  PA/MA low bits are ignored for 16/32-bit sizes (13.4.5/13.4.6: aligned access).
* AHB/APB byte lanes (13.3.4): a byte or half-word write to an address on APB1/APB2 is performed as a 32-bit write of
  the data replicated on all lanes (0xB0 -> 0xB0B0B0B0, 0xB1B0 -> 0xB1B0B1B0) to the word address.
* Flags (13.4.1/13.4.2): ISR GIFx/TCIFx/HTIFx/TEIFx are read-only; GIFx is a stored flag set with any of TE/HT/TC and
  cleared only by CGIFx (CGIFx clears all four; CTCIFx/CHTIFx/CTEIFx one each).  IFCR reads 0.  Bits of channels 6/7
  are reserved on DMA2 (13.4 note), as are its CCR6/7 .. CMAR6/7 (read 0, writes ignored).
* Interrupts (13.3.6): a channel line is (TCIF & TCIE) | (HTIF & HTIE) | (TEIF & TEIE); vector "DMAx_Channely" from the
  chip YAML, DMA2 channels 4 and 5 sharing "DMA2_Channel4_5" on high/XL density (separate vectors on the connectivity
  line).
* CNDTR (13.4.4): 16 bits, writable only while the channel is disabled ("read-only" once enabled); with CNDTR = 0 "no
  transaction can be served whether the channel is enabled or not".  TCIF at CNDTR = 0.
* Transfer error (13.3.5): an access to a reserved address (BusError: unmapped and outside the auto-stub windows) sets
  TEIF (and GIF) and clears EN.
Approximate / not in RM0008:
* HTIF is set when CNDTR reaches N // 2 (13.3.3 "once half of the bytes are transferred"; for odd N the RM gives no
  rule; N = 1 sets no HTIF).
* CPAR/CMAR writes while EN = 1 are ignored (13.4.5/13.4.6 only say they "must not be written"); CCR configuration
  bits other than EN/TCIE/HTIE/TEIE written while EN = 1 are ignored with one `warn` (RM0008 states no lock; later ST
  manuals for the same DMA IP make them read-only while EN = 1).  PSIZE/MSIZE = 11 (reserved) refuses the enable.
* Transfers take zero bus time (no CPU stall or bus-matrix round robin, 13.3) but never run inside the MMIO access that
  triggered them (a service event at the current cycle).
* Clock gate RCC_AHBENR.DMAxEN: a register access with the clock off logs one `warn` (transfers still run).
"""
from __future__ import annotations

from typing import Any

from ...bus import BusError
from ..base import MMIOBlock, pure_read
from ..registry import register
from ._util import GatedBlock, build_regs, irq_number

NCH = 7

# RM0008 13.3.7 Table 78 "Summary of DMA1 requests for each channel", channel 1..7.  TIM4 / USART3 / SPI2 / I2C2
# requests are listed although the low-density F103T6 has none of those peripherals (medium/high density parts).
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
# RM0008 13.3.7 Table 79 "Summary of DMA2 requests for each channel" (high-density, XL-density and connectivity line;
# ADC3, SDIO and TIM8 requests exist on high/XL density only).  The DAC channels request as "DAC1"/"DAC2" (stm32.dac)
# and share channels 3/4 with TIM6_UP/TIM7_UP.
REQUESTS2: dict[int, tuple[str, ...]] = {
    1: ("TIM5_CH4", "TIM5_TRIG", "TIM8_CH3", "TIM8_UP", "SPI3_RX", "I2S3_RX"),
    2: ("TIM8_CH4", "TIM8_TRIG", "TIM8_COM", "TIM5_CH3", "TIM5_UP", "SPI3_TX", "I2S3_TX"),
    3: ("TIM8_CH1", "UART4_RX", "TIM6_UP", "DAC1", "DAC_Channel1"),
    4: ("TIM5_CH2", "SDIO", "TIM7_UP", "DAC2", "DAC_Channel2"),
    5: ("ADC3", "TIM8_CH2", "TIM5_CH1", "UART4_TX"),
}
CHANNEL_OF2 = {n: ch for ch, names in REQUESTS2.items() for n in names}
# AHB-to-APB bridges (RM0008 3.3 memory map: APB1 0x4000_0000-0x4000_7FFF, APB2 0x4001_0000-0x4001_7FFF; the AHB
# peripherals start with SDIO at 0x4001_8000): byte / half-word DMA writes there become word writes with the data
# replicated on all byte lanes (13.3.4).
APB_RANGES = ((0x40000000, 0x40018000),)

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
        params.setdefault("reserved_zero", True)        # reserved offsets read 0, writes ignored
        self.gate = str(params.get("gate", name))
        digits = "".join(ch for ch in name if ch.isdigit())
        self.controller = int(params.get("controller", digits or 1))
        if self.controller not in (1, 2):
            raise ValueError(f"{name}: stm32f1.dma controller must be 1 or 2")
        self.nch = int(params.get("channels", 7 if self.controller == 1 else 5))
        if not 1 <= self.nch <= NCH:
            raise ValueError(f"{name}: stm32f1.dma channels must be 1..7")
        table = REQUESTS if self.controller == 1 else REQUESTS2
        self.requests = {c: names for c, names in table.items() if c <= self.nch}
        self.channel_of = {n: c for c, names in self.requests.items() for n in names}
        self.lane_replication = bool(params.get("apb_byte_lanes", True))
        self.apb_ranges = tuple(tuple(int(x) for x in r) for r in params.get("apb_ranges", APB_RANGES))
        # ISR/IFCR bits of channels this controller does not have are reserved (13.4: "all bits related to channel6
        # and channel7 are not relevant for DMA2"); bits 31:28 are reserved on both.
        self.flag_mask = (1 << (4 * self.nch)) - 1
        ov: dict[str, dict] = {
            "ISR": {"ro_mask": 0xFFFFFFFF},
            "IFCR": {"on_write": self._ifcr_write, "on_read": pure_read(lambda b, r: 0)},
        }
        for c in range(1, NCH + 1):
            if c > self.nch:                 # absent channel: reserved, reads 0
                for k in ("CCR", "CNDTR", "CPAR", "CMAR"):
                    ov[f"{k}{c}"] = {"ro_mask": 0xFFFFFFFF, "reset": 0}
                continue
            ov[f"CCR{c}"] = {"on_write": self._ccr_write(c), "ro_mask": 0xFFFF8000}
            ov[f"CNDTR{c}"] = {"on_write": self._cndtr_write(c), "ro_mask": 0xFFFF0000}
            ov[f"CPAR{c}"] = {"on_write": self._prot_write(c)}
            ov[f"CMAR{c}"] = {"on_write": self._prot_write(c)}
        r = build_regs((name, f"DMA{self.controller}", "DMA1"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.known = set(self.channel_of)
        self.levels: dict[str, bool] = {}
        self.chans = [None] + [_Chan(c) for c in range(1, self.nch + 1)]
        self._ccr = [None] + [self.regs[f"CCR{c}"] for c in range(1, self.nch + 1)]
        self._cndtr = [None] + [self.regs[f"CNDTR{c}"] for c in range(1, self.nch + 1)]
        self._ev = None
        self._busy = False
        self._first: Any = None             # DMA1 when this is DMA2 (13.3.2: DMA1 has priority over DMA2)
        self._irq_lines: dict[int, set[int]] = {}
        self.items = 0                      # data items moved (statistics)

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        k = self.controller
        for c in range(1, self.nch + 1):
            names = [f"{self.name}_Channel{c}", f"DMA{k}_Channel{c}"]
            if k == 2 and c in (4, 5):       # high/XL density: one vector "DMA2_Channel4_5" (13.3.6 note)
                names += [f"{self.name}_Channel4_5", "DMA2_Channel4_5"]
            self.chans[c].irq = irq_number(chip, *names)

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
        self._irq_lines.clear()
        super().reset()

    def write(self, offset: int, size: int, value: int) -> None:
        if self._gate_loc is not None:
            self._gate_ok()
        MMIOBlock.write(self, offset, size, value)

    # ------------------------------------------------------------------ request API (stm32.dma compatible)
    def request(self, name: str, level: bool = True, last: bool = False) -> bool:
        """Assert (level=True) or deassert peripheral DMA request `name` (RM0008 Table 78).  False when no channel of
        this controller serves it."""
        c = self.channel_of.get(name)
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
        return self.requests[c]

    def remaining_items(self, name: str) -> int | None:
        """CNDTR of the enabled channel serving request `name`, None if none (stm32.i2c CR2.LAST uses it)."""
        c = self.channel_of.get(name)
        if c is None or not self._ccrv(c) & EN:
            return None
        return self._ndt(c)

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
                srcs = self._irq_lines.setdefault(ch.irq, set())   # DMA2_Channel4_5: one line, two channels
                if lvl:
                    srcs.add(c)
                else:
                    srcs.discard(c)
                self.irq(ch.irq, bool(srcs))

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
        for c in range(1, self.nch + 1):
            nib = (new >> (4 * (c - 1))) & 0xF
            if nib & F_G:
                nib = 0xF                    # CGIFx clears GIF, TCIF, HTIF and TEIF of the channel
            isr &= ~(nib << (4 * (c - 1)))
        self.set("ISR", isr)
        for c in range(1, self.nch + 1):
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
        if self._ndt(c) and (ch.m2m or any(self.levels.get(n) for n in self.requests[c])):
            self._kick()

    # ------------------------------------------------------------------ bus access
    def _rd(self, addr: int, n: int) -> int:
        try:
            return self.chip.bus.read(addr, n)
        except BusError as e:
            raise _DmaFault(addr, str(e)) from None

    def _wr(self, addr: int, n: int, v: int) -> None:
        if n < 4 and self.lane_replication:
            for lo, hi in self.apb_ranges:
                if lo <= addr < hi:          # 13.3.4: the AHB/APB bridge ignores HSIZE: word write, lanes replicated
                    v = v * (0x01010101 if n == 1 else 0x00010001)
                    addr &= ~3
                    n = 4
                    break
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
        for n in self.requests[c]:
            if levels.get(n):
                return True
        return False

    def _service(self) -> None:
        self._ev = None
        first = self._dma1()
        if first is not None and first._ev is not None and not first._busy:
            self.chip.sched.cancel(first._ev)        # 13.3.2 note: DMA1 is served before DMA2
            first._service()
        self._busy = True
        try:
            guard = 0
            while guard < 2_000_000:
                best, best_pl = 0, -1
                for c in range(1, self.nch + 1):
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
            for n in self.requests[c]:            # acknowledge before the access: a re-request during it is kept
                self.levels[n] = False
        # 13.4.5/13.4.6: PA[0] / MA[0] (16-bit) and PA[1:0] / MA[1:0] (32-bit) are ignored (aligned access)
        pa = ch.pptr & ~(ch.psize - 1)
        ma = ch.mptr & ~(ch.msize - 1)
        try:
            if ch.dir:                       # memory -> peripheral
                v = self._rd(ma, ch.msize)
                self._wr(pa, ch.psize, v & ((1 << (8 * ch.psize)) - 1))
            else:                            # peripheral (or M2M source) -> memory
                v = self._rd(pa, ch.psize)
                self._wr(ma, ch.msize, v & ((1 << (8 * ch.msize)) - 1))
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
                self._notify_last(self.requests[c])
        if flags:
            self._set_flags(c, flags)

    def _dma1(self) -> Any:
        """DMA1 of the chip when this block is DMA2 (cached), else None."""
        if self.controller != 2 or self.chip is None:
            return None
        if self._first is None:
            self._first = next((b for b in (getattr(self.chip, "peripherals", {}) or {}).values()
                                if getattr(b, "model", None) == self.model and getattr(b, "controller", 0) == 1),
                               False)
        return self._first or None

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
