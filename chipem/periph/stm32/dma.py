"""STM32F4 DMA controller (DMA1/DMA2, RM0090 ch. 10): 8 streams x 8 channels, request mapping per Tables 43/44
(RM0090 Rev 22 numbering, including the requests that exist on the STM32F42xxx/43xxx only: SAI1_A/B, SPI4-6,
UART7/8 -- table note 1; no other part's model raises them).

Request API (used by the stm32 peripheral models; also reachable through `_util.dma_request(chip, name, level)`):

    DMA.request(name: str, level: bool) -> bool

`name` is the request name of RM0090 Table 43 (DMA1) / Table 44 (DMA2): "USART1_RX", "USART1_TX", "UART4_TX",
"TIM3_CH1", "TIM3_UP", "TIM3_TRIG", "TIM1_COM", "ADC1", "SPI1_RX", "I2C1_TX", "DAC1", "SDIO", "DCMI" ...  Table cells
that hold several requests ("TIM2_UP/TIM2_CH3") are ORed onto one stream request line, so either name works.  A
request that is not in this controller's table is ignored (returns False); the other controller may take it, which is
why peripherals call `_util.dma_request(chip, name, level)` (both controllers).  level=True asserts the request (or
re-arms it); it stays asserted until the peripheral calls request(name, False) or the DMA acknowledges it by
transferring ONE data item on a stream that is enabled with that request selected (CHSEL).  So a peripheral may
either drive a level (USART: TXE & DMAT, deasserted by the DR write the DMA itself performs) or pulse it once per
event (timer update: request(name, True) on every UEV, no deassert needed).  A request asserted while no stream
serves it is remembered, and a stream enabled later picks it up (silicon's request lines are levels).  Peripherals
deassert their own requests in their reset(); the DMA's reset (chip reset, RCC_AHB1RSTR) keeps the request levels.
The memory-to-memory pseudo request "MEM2MEM" is accepted (no-op): M2M streams run without a request.

Transfers take zero bus time, like kinetis.dma, but never run inside the MMIO access that triggered them: a request
or the EN write schedules a service event at the current cycle (`sched.in_cycles(0)`, which ends the running slice
after the access), so flags, NDTR and the stream IRQs change after the triggering store has completed.  In one
service pass the controller arbitrates like silicon (RM0090 10.3.3): the ready stream with the highest PL wins, the
lower stream number on a tie; each grant moves one data item (peripheral-driven streams then wait for the next
request; a memory-to-memory stream stays ready, so a higher-priority M2M stream completes before a lower one moves).

Per stream (SxCR, SxNDTR, SxPAR, SxM0AR, SxM1AR, SxFCR; registers from the SVD):
  * DIR P2M (00), M2P (01), M2M (10, DMA2 only: on DMA1 it is a transfer error, RM0090 10.3.6; DMDIS forced to 1
    at enable).  CHSEL picks the request (Tables 43/44, `REQUESTS`).  PL 0..3.
  * PFCTRL (peripheral flow control, SDIO only, RM0090 10.3.15): NDTR is forced to 0xFFFF at enable, CIRC (and DBM,
    forbidden by Table 50) forced to 0; the stream completes (TCIF, EN cleared, FIFO flushed) after serving the
    request the peripheral flagged as its last (`request(name, True, last=True)`), or when NDTR reaches 0.  The data
    count is 0xFFFF - NDTR.  In M2M mode PFCTRL is forced to 0.
  * NDTR = 0 when EN is set: the register is reloaded with the value software last wrote (RM0090 10.5.6, e.g.
    re-enabling a completed stream repeats the same count).  PINCOS is forced to 0 in direct mode or with PBURST != 0.
  * NDTR counts peripheral-size items (PSIZE) down; PINC/MINC increment by PSIZE/MSIZE (PINCOS: the peripheral
    address steps by 4 whatever PSIZE is; forced to 0 in direct mode).  SxPAR/SxM0AR/SxM1AR are not modified by the
    transfer (internal pointers, as on silicon).  At NDTR = 0: TCIF; in circular mode (CIRC, forced to 1 by DBM at
    enable) NDTR and the pointers reload and, in double-buffer mode, CT toggles to the other memory buffer;
    otherwise EN clears.  HTIF when NDTR reaches half of the programmed count (NDTR == N // 2).
  * Direct mode (FCR.DMDIS = 0): MSIZE is forced to PSIZE (and PBURST/MBURST to single) when EN is set; each request
    moves one item straight through.  FIFO mode (DMDIS = 1): a 16-byte FIFO packs/unpacks between PSIZE and MSIZE
    with the same data as silicon: P2M/M2M data goes to memory in MSIZE units whenever the FIFO reaches the FTH
    threshold, at the end of the transfer (or of each circular/double-buffer cycle) and when the stream is disabled
    (RM0090 10.3.12 "FIFO flush": fewer bytes than MSIZE are still written as one MSIZE unit, the missing bytes as 0
    -- silicon writes "an undesired value"); M2P reads MSIZE units from memory when the FIFO cannot supply
    the next PSIZE item.  FS reports the FIFO level.  Burst beats/timing are not modelled.  An FTH/MBURST/MSIZE
    combination forbidden by RM0090 Table 49 sets FEIF and the stream is not enabled.
  * EN: SxCR (except EN and the interrupt enables), NDTR, PAR, FCR (except FEIE) and, unless in double-buffer mode,
    M0AR/M1AR are write-protected while EN = 1 (writes ignored).  In double-buffer mode, writing the memory address
    register in use (M0AR with CT = 0, M1AR with CT = 1) is refused with TEIF and the stream is disabled.  Clearing EN
    stops the stream after the current item (immediately here): the FIFO is flushed to memory (P2M), TCIF is set,
    NDTR keeps the remaining count and EN reads 0.  Enabling a stream whose flags are still set works but logs one
    `warn` (RM0090: clear them before setting EN).
  * Flags TCIF/HTIF/TEIF/DMEIF/FEIF in LISR (streams 0-3) / HISR (4-7), cleared by writing 1 to LIFCR/HIFCR (those
    read 0).  Stream IRQ "<DMAx>_Stream<n>" (looked up by name in the chip YAML) is the level of (TCIF & TCIE) |
    (HTIF & HTIE) | (TEIF & TEIE) | (DMEIF & DMEIE) | (FEIF & FCR.FEIE).
  * TEIF on a bus error: an access the bus cannot resolve (BusError, outside the auto-stub windows) or to a memory the
    DMA cannot reach (the CCM data RAM, any memory region named "CCM*"; RM0090 2.3: CPU-only) sets TEIF and clears
    EN.  DMEIF and FIFO overrun/underrun FEIF cannot occur with zero-time transfers and are never set.
Clock gate RCC_AHB1ENR DMA1EN/DMA2EN: a gated-off register access logs one `warn` (transfers still run, like the rest
of the family); RCC_AHB1RSTR resets the controller (all streams disabled, flags cleared).
"""
from __future__ import annotations

from typing import Any

from ...bus import BusError
from ..base import MMIOBlock
from ..registry import register
from ._util import GatedBlock, build_regs, irq_number

NSTREAMS = 8

# RM0090 Table 43 "DMA1 request mapping": REQ1[channel][stream] (None = no request)
_T42 = [
    ("SPI3_RX", None, "SPI3_RX", "SPI2_RX", "SPI2_TX", "SPI3_TX", None, "SPI3_TX"),
    ("I2C1_RX", None, "TIM7_UP", None, "TIM7_UP", "I2C1_RX", "I2C1_TX", "I2C1_TX"),
    ("TIM4_CH1", None, "I2S3_EXT_RX", "TIM4_CH2", "I2S2_EXT_TX", "I2S3_EXT_TX", "TIM4_UP", "TIM4_CH3"),
    ("I2S3_EXT_RX", "TIM2_UP/TIM2_CH3", "I2C3_RX", "I2S2_EXT_RX", "I2C3_TX", "TIM2_CH1", "TIM2_CH2/TIM2_CH4",
     "TIM2_UP/TIM2_CH4"),
    ("UART5_RX", "USART3_RX", "UART4_RX", "USART3_TX", "UART4_TX", "USART2_RX", "USART2_TX", "UART5_TX"),
    ("UART8_TX", "UART7_TX", "TIM3_CH4/TIM3_UP", "UART7_RX", "TIM3_CH1/TIM3_TRIG", "TIM3_CH2", "UART8_RX",
     "TIM3_CH3"),
    ("TIM5_CH3/TIM5_UP", "TIM5_CH4/TIM5_TRIG", "TIM5_CH1", "TIM5_CH4/TIM5_TRIG", "TIM5_CH2", None, "TIM5_UP", None),
    (None, "TIM6_UP", "I2C2_RX", "I2C2_RX", "USART3_TX", "DAC1", "DAC2", "I2C2_TX"),
]
# RM0090 Table 44 "DMA2 request mapping"
_T43 = [
    ("ADC1", "SAI1_A", "TIM8_CH1/TIM8_CH2/TIM8_CH3", "SAI1_A", "ADC1", "SAI1_B", "TIM1_CH1/TIM1_CH2/TIM1_CH3", None),
    (None, "DCMI", "ADC2", "ADC2", "SAI1_B", "SPI6_TX", "SPI6_RX", "DCMI"),
    ("ADC3", "ADC3", None, "SPI5_RX", "SPI5_TX", "CRYP_OUT", "CRYP_IN", "HASH_IN"),
    ("SPI1_RX", None, "SPI1_RX", "SPI1_TX", None, "SPI1_TX", None, None),
    ("SPI4_RX", "SPI4_TX", "USART1_RX", "SDIO", None, "USART1_RX", "SDIO", "USART1_TX"),
    (None, "USART6_RX", "USART6_RX", "SPI4_RX", "SPI4_TX", None, "USART6_TX", "USART6_TX"),
    ("TIM1_TRIG", "TIM1_CH1", "TIM1_CH2", "TIM1_CH1", "TIM1_CH4/TIM1_TRIG/TIM1_COM", "TIM1_UP", "TIM1_CH3", None),
    (None, "TIM8_UP", "TIM8_CH1", "TIM8_CH2", "TIM8_CH3", "SPI5_RX", "SPI5_TX", "TIM8_CH4/TIM8_TRIG/TIM8_COM"),
]


def _split(table) -> list[list[tuple[str, ...]]]:
    """REQUESTS[stream][chsel] = tuple of request names ORed onto that stream request line."""
    return [[tuple(table[ch][s].split("/")) if table[ch][s] else () for ch in range(8)] for s in range(NSTREAMS)]


REQUESTS = {1: _split(_T42), 2: _split(_T43)}

# SxCR
EN, DMEIE, TEIE, HTIE, TCIE, PFCTRL = 1 << 0, 1 << 1, 1 << 2, 1 << 3, 1 << 4, 1 << 5
CIRC, PINC, MINC, PINCOS, DBM, CT = 1 << 8, 1 << 9, 1 << 10, 1 << 15, 1 << 18, 1 << 19
IE_BITS = DMEIE | TEIE | HTIE | TCIE
CR_FREE = EN | IE_BITS                    # writable while the stream is enabled
DIR_P2M, DIR_M2P, DIR_M2M = 0, 1, 2
# SxFCR
FTH_MASK, DMDIS, FS_MASK, FEIE = 3, 1 << 2, 7 << 3, 1 << 7
# flag bits relative to the stream's base bit in LISR/HISR
F_FE, F_DME, F_TE, F_HT, F_TC = 1, 4, 8, 16, 32
F_ALL = F_FE | F_DME | F_TE | F_HT | F_TC
_FLAG_SHIFT = (0, 6, 16, 22)
_SIZES = (1, 2, 4, 0)                    # PSIZE/MSIZE 11 = reserved
_BURST = (1, 4, 8, 16)


def _fifo_cfg_ok(msize: int, mburst: int, fth: int) -> bool:
    """RM0090 Table 49: a memory burst must fit the FIFO threshold (threshold bytes a multiple of the burst)."""
    beats = _BURST[mburst]
    if beats == 1:
        return True
    return (4 * (fth + 1)) % (beats * msize) == 0


class _Stream:
    __slots__ = ("idx", "n0", "pptr", "mptr", "fifo", "mleft", "warned_flags", "pf_warned", "dir", "psize",
                 "msize", "pstep", "mstep", "circ", "fifo_mode", "thresh", "irq", "line", "names",
                 "prog", "pfc")

    def __init__(self, idx: int):
        self.idx = idx
        self.irq: int | None = None
        self.line = False
        self.pf_warned = False
        self.prog = 0                     # NDTR value last written by software (reloaded by EN, RM0090 10.5.6)
        self.clear()

    def clear(self) -> None:
        self.n0 = 0                       # programmed NDTR (reload value)
        self.pptr = 0
        self.mptr = 0
        self.fifo = bytearray()
        self.mleft = 0                    # M2P FIFO mode: memory bytes not yet fetched in this cycle
        self.warned_flags = False
        self.dir = DIR_P2M
        self.psize = self.msize = 1
        self.pstep = self.mstep = 0
        self.circ = False
        self.fifo_mode = False
        self.thresh = 4
        self.names: tuple[str, ...] = ()
        self.pfc = False                  # peripheral is the flow controller (PFCTRL)


@register("stm32.dma")
class DMA(GatedBlock):
    model = "stm32.dma"

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        ctl = params.get("controller")
        if ctl is None:
            digits = "".join(ch for ch in name if ch.isdigit())
            ctl = int(digits) if digits else 1
        self.controller = int(ctl)
        if self.controller not in REQUESTS:
            raise ValueError(f"{name}: params.controller must be 1 or 2, not {ctl!r}")
        self.gate = name
        self.m2m_ok = bool(params.get("m2m", self.controller == 2))
        ov: dict[str, dict] = {
            "LISR": {"ro_mask": 0xFFFFFFFF}, "HISR": {"ro_mask": 0xFFFFFFFF},
            "LIFCR": {"on_write": self._ifcr_write(0), "on_read": lambda b, r: 0},
            "HIFCR": {"on_write": self._ifcr_write(4), "on_read": lambda b, r: 0},
        }
        for s in range(NSTREAMS):
            ov[f"S{s}CR"] = {"on_write": self._cr_write(s)}
            ov[f"S{s}NDTR"] = {"on_write": self._ndtr_write(s), "ro_mask": 0xFFFF0000}
            ov[f"S{s}PAR"] = {"on_write": self._prot_write(s)}
            ov[f"S{s}M0AR"] = {"on_write": self._mar_write(s, 0)}
            ov[f"S{s}M1AR"] = {"on_write": self._mar_write(s, 1)}
            ov[f"S{s}FCR"] = {"on_write": self._fcr_write(s), "ro_mask": FS_MASK}
        r = build_regs((name, f"DMA{self.controller}", "DMA2"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.table = REQUESTS[self.controller]
        self.known = {n for row in self.table for cell in row for n in cell}
        self.levels: dict[str, bool] = {}
        self.lasts: dict[str, bool] = {}
        self.streams = [_Stream(s) for s in range(NSTREAMS)]
        self._cr = [self.regs[f"S{s}CR"] for s in range(NSTREAMS)]
        self._ev = None
        self._busy = False
        self._no_access: list[tuple[int, int]] = []
        self.items = 0                    # data items moved (statistics)

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        for st in self.streams:
            st.irq = irq_number(chip, f"{self.name}_Stream{st.idx}", f"DMA{self.controller}_Stream{st.idx}")
        self._no_access = []
        for m in getattr(getattr(chip, "desc", None), "memory", None) or []:
            if str(getattr(m, "name", "")).upper().startswith("CCM"):
                self._no_access.append((m.base, m.base + m.size))
        for lo, hi in self.params.get("no_access", None) or []:
            self._no_access.append((int(lo), int(hi)))

    def reset(self) -> None:
        if self._ev is not None and self.chip is not None:
            self.chip.sched.cancel(self._ev)
        self._ev = None
        self._busy = False
        for st in self.streams:
            if st.line and st.irq is not None and self.chip is not None:
                self.irq(st.irq, False)
            st.line = False
            st.prog = 0
            st.clear()
        super().reset()

    def write(self, offset: int, size: int, value: int) -> None:
        if self._gate_loc is not None:
            self._gate_ok()
        MMIOBlock.write(self, offset, size, value)

    # ------------------------------------------------------------------ request API
    def request(self, name: str, level: bool = True, last: bool = False) -> bool:
        """A peripheral asserts (level=True) or deasserts DMA request `name` (RM0090 Table 43/44 name). Returns
        False when this controller has no stream/channel for it (ignored).  `last`: the request is the peripheral's
        "last data" signal (RM0090 10.3.15, SDIO only): a stream with PFCTRL = 1 completes after serving it."""
        if name not in self.known:
            return name == "MEM2MEM"
        if not level:
            self.levels[name] = False
            self.lasts[name] = False
            return True
        self.levels[name] = True
        self.lasts[name] = bool(last)
        for s in range(NSTREAMS):
            cr = self._crv(s)
            if cr & EN and name in self.table[s][cr >> 25 & 7]:
                self._kick()
                break
        return True

    def request_active(self, name: str) -> bool:
        return bool(self.levels.get(name))

    def stream_request(self, s: int) -> tuple[str, ...]:
        """Request names selected by stream s (CHSEL)."""
        return self.table[s][self._crv(s) >> 25 & 7]

    # ------------------------------------------------------------------ helpers
    def _crv(self, s: int) -> int:
        r = self._cr[s]
        return int.from_bytes(self.mem[r.boff:r.boff + 4], "little")

    def _sreg(self, s: int, reg: str) -> int:
        return self.get(f"S{s}{reg}")

    def _flag_loc(self, s: int) -> tuple[str, int]:
        return ("LISR" if s < 4 else "HISR"), _FLAG_SHIFT[s & 3]

    def flags(self, s: int) -> int:
        """Stream s flags as F_FE | F_DME | F_TE | F_HT | F_TC bits (0..0x3D)."""
        reg, sh = self._flag_loc(s)
        return (self.get(reg) >> sh) & F_ALL

    def _set_flags(self, s: int, bits: int) -> None:
        reg, sh = self._flag_loc(s)
        self.set(reg, self.get(reg) | (bits << sh))
        self._update_line(s)

    def _update_line(self, s: int) -> None:
        st = self.streams[s]
        f = self.flags(s)
        cr = self._crv(s)
        lvl = bool((f & F_TC and cr & TCIE) or (f & F_HT and cr & HTIE) or (f & F_TE and cr & TEIE)
                   or (f & F_DME and cr & DMEIE) or (f & F_FE and self._sreg(s, "FCR") & FEIE))
        if lvl != st.line:
            st.line = lvl
            if st.irq is not None and self.chip is not None:
                self.irq(st.irq, lvl)

    lp_frozen: Any = False              # PWR freezer: Stop / gated Sleep (AHB clock off)

    def time_shift(self, dns: float, dcycles: int) -> None:
        """Clock back: requests asserted while the controller had no clock are served now (the request lines are
        levels, RM0090 10.3.5)."""
        self._kick()

    def _kick(self) -> None:
        if self.lp_frozen:
            return                      # no HCLK: the asserted request waits for the clock (time_shift)
        if self._ev is None and not self._busy and self.chip is not None:
            self._ev = self.chip.sched.in_cycles(0, self._service)

    def _set_cr(self, s: int, value: int) -> None:
        self._store(self._cr[s], value)

    def _set_fs(self, s: int) -> None:
        st = self.streams[s]
        n = len(st.fifo)
        fs = 4 if n == 0 else 5 if n >= 16 else n // 4
        self.set(f"S{s}FCR", (self._sreg(s, "FCR") & ~FS_MASK) | fs << 3)

    # ------------------------------------------------------------------ register hooks
    def _ifcr_write(self, first: int):
        def wr(blk, reg, old, new):
            sreg = "LISR" if first == 0 else "HISR"
            self.set(sreg, self.get(sreg) & ~new)
            for s in range(first, first + 4):
                self._update_line(s)
            return 0
        return wr

    def _prot_write(self, s: int):
        def wr(blk, reg, old, new):
            if self._crv(s) & EN:
                return old                # protected while the stream is enabled
            return None
        return wr

    def _ndtr_write(self, s: int):
        def wr(blk, reg, old, new):
            if self._crv(s) & EN:
                return old                # protected while the stream is enabled
            self.streams[s].prog = new & 0xFFFF
            return None
        return wr

    def _mar_write(self, s: int, which: int):
        def wr(blk, reg, old, new):
            cr = self._crv(s)
            if not cr & EN:
                return None
            if not cr & DBM:
                return old
            if bool(cr & CT) == bool(which):          # the buffer in use: refused, transfer error
                self.emit("warn", block=self.name, msg=f"{self.name} stream {s}: write to M{which}AR while it is the "
                          "current double-buffer target (CT); TEIF set, stream disabled")
                self._error(s)
                return old
            return None
        return wr

    def _fcr_write(self, s: int):
        def wr(blk, reg, old, new):
            if self._crv(s) & EN:
                new = (old & ~FEIE) | (new & FEIE)
            self._store(reg, new)
            self._update_line(s)
            return new
        return wr

    def _cr_write(self, s: int):
        def wr(blk, reg, old, new):
            if old & EN:
                new = (old & ~CR_FREE) | (new & CR_FREE)
                self._store(reg, new)
                if not new & EN:
                    self._disable(s)
                self._update_line(s)
                return None
            if new & EN:
                self._store(reg, new)
                self._enable(s)
            self._update_line(s)
            return None
        return wr

    # ------------------------------------------------------------------ enable / disable
    def _enable(self, s: int) -> None:
        st = self.streams[s]
        cr = self._crv(s)
        fcr = self._sreg(s, "FCR")
        direction = cr >> 6 & 3
        if self.flags(s) and not st.warned_flags:
            st.warned_flags = True
            self.emit("warn", block=self.name, msg=f"{self.name} stream {s} enabled with its LISR/HISR flags still set "
                      f"(0x{self.flags(s):02x}); RM0090 requires clearing them first")
        if direction == 3:
            self.emit("warn", block=self.name, msg=f"{self.name} stream {s}: DIR=11 is reserved; stream not enabled")
            self._set_cr(s, cr & ~EN)
            return
        if direction == DIR_M2M:
            fcr |= DMDIS                                  # direct mode is not allowed in M2M (hardware sets DMDIS)
            cr &= ~PFCTRL                                 # RM0090 10.5.5: PFCTRL forced to 0 in M2M
            if cr & (CIRC | DBM):
                self.emit("warn", block=self.name, msg=f"{self.name} stream {s}: circular/double-buffer mode is not "
                          "allowed in memory-to-memory mode; running a single transfer")
                cr &= ~(CIRC | DBM)
        if cr & PFCTRL:
            if cr & DBM and not st.pf_warned:
                st.pf_warned = True
                self.emit("warn", block=self.name, msg=f"{self.name} stream {s}: double-buffer mode is forbidden with "
                          "peripheral flow control (RM0090 Table 50); DBM ignored")
            cr &= ~(CIRC | DBM)                           # RM0090 10.5.5: CIRC forced to 0 with PFCTRL
        elif cr & DBM:
            cr |= CIRC                                    # RM0090: CIRC forced to 1 when DBM is set and EN = 1
        if cr >> 21 & 3:
            cr &= ~PINCOS                                 # RM0090 10.5.5: PINCOS forced low with PBURST != 00
        fifo_mode = bool(fcr & DMDIS)
        psz = cr >> 11 & 3
        if not fifo_mode:
            cr = (cr & ~(3 << 13 | 3 << 21 | 3 << 23 | PINCOS)) | psz << 13     # MSIZE := PSIZE, single bursts
        msz = cr >> 13 & 3
        psize, msize = _SIZES[psz], _SIZES[msz]
        if not psize or not msize:
            self.emit("warn", block=self.name, msg=f"{self.name} stream {s}: PSIZE/MSIZE = 11 is reserved; "
                      "stream not enabled")
            self._set_cr(s, cr & ~EN)
            return
        if fifo_mode and not _fifo_cfg_ok(msize, cr >> 23 & 3, fcr & FTH_MASK):
            self.emit("warn", block=self.name, msg=f"{self.name} stream {s}: FIFO threshold FTH={fcr & FTH_MASK} "
                      f"incompatible with MBURST={cr >> 23 & 3} / MSIZE={msize} (RM0090 Table 49): FEIF, not enabled")
            self.set(f"S{s}FCR", fcr)
            self._set_cr(s, cr & ~EN)
            self._set_flags(s, F_FE)
            return
        if direction == DIR_M2M and not self.m2m_ok:
            self.emit("warn", block=self.name, msg=f"{self.name} stream {s}: memory-to-memory transfers are only "
                      "possible on DMA2 (RM0090 10.3.6): transfer error")
            self.set(f"S{s}FCR", fcr)
            self._set_cr(s, cr)
            self._error(s)
            return
        self.set(f"S{s}FCR", fcr)
        self._set_cr(s, cr)
        st.dir = direction
        st.psize, st.msize = psize, msize
        st.pstep = (4 if cr & PINCOS and fifo_mode else psize) if cr & PINC else 0
        st.mstep = msize if cr & MINC else 0
        st.circ = bool(cr & CIRC)
        st.fifo_mode = fifo_mode
        st.thresh = 4 * ((fcr & FTH_MASK) + 1)
        st.names = self.table[s][cr >> 25 & 7]
        st.pfc = bool(cr & PFCTRL)
        if st.pfc:
            self.set(f"S{s}NDTR", 0xFFFF)                # RM0090 10.3.15: forced to 0xFFFF at enable
        elif not self._sreg(s, "NDTR") & 0xFFFF:
            self.set(f"S{s}NDTR", st.prog)               # RM0090 10.5.6: reloaded with the programmed value
        st.n0 = self._sreg(s, "NDTR") & 0xFFFF
        st.fifo = bytearray()
        self._load_ptrs(s, cr)
        self._set_fs(s)
        if st.n0 and (direction == DIR_M2M or any(self.levels.get(n) for n in st.names)):
            self._kick()

    def _load_ptrs(self, s: int, cr: int) -> None:
        st = self.streams[s]
        st.pptr = self._sreg(s, "PAR")
        st.mptr = self._sreg(s, "M1AR" if (cr & DBM and cr & CT) else "M0AR")
        st.mleft = st.n0 * st.psize

    def _disable(self, s: int) -> None:
        """Software cleared EN: stop after the current item (now), flush the FIFO, TCIF."""
        st = self.streams[s]
        if st.fifo and st.dir != DIR_M2P:
            try:
                self._flush(s, all_=True)
            except _DmaFault as e:
                self._fault(s, e)
                return
        st.fifo = bytearray()
        self._set_fs(s)
        self._set_flags(s, F_TC)

    def _error(self, s: int) -> None:
        self._set_cr(s, self._crv(s) & ~EN)
        self.streams[s].fifo = bytearray()
        self._set_fs(s)
        self._set_flags(s, F_TE)

    # ------------------------------------------------------------------ bus access
    def _check(self, addr: int, n: int) -> None:
        for lo, hi in self._no_access:
            if addr < hi and addr + n > lo:
                raise _DmaFault(addr, "not reachable by the DMA (CPU-only memory)")

    def _rd(self, addr: int, n: int) -> int:
        self._check(addr, n)
        try:
            return self.chip.bus.read(addr, n)
        except BusError as e:
            raise _DmaFault(addr, str(e)) from None

    def _wr(self, addr: int, n: int, v: int) -> None:
        self._check(addr, n)
        try:
            self.chip.bus.write(addr, n, v)
        except BusError as e:
            raise _DmaFault(addr, str(e)) from None

    def _fault(self, s: int, e: "_DmaFault") -> None:
        self.emit("warn", block=self.name, msg=f"{self.name} stream {s}: bus error at 0x{e.addr:08x} ({e.msg}); "
                  "TEIF set, stream disabled")
        self._error(s)

    # ------------------------------------------------------------------ transfer engine
    def _ready(self, s: int, cr: int) -> bool:
        st = self.streams[s]
        if not cr & EN or not st.n0 or not self._sreg(s, "NDTR") & 0xFFFF:
            return False
        if st.dir == DIR_M2M:
            return True
        levels = self.levels
        for n in st.names:
            if levels.get(n):
                return True
        return False

    def _service(self) -> None:
        self._ev = None
        self._busy = True
        try:
            guard = 0
            while guard < 2_000_000:
                best, best_pl = -1, -1
                for s in range(NSTREAMS):
                    cr = self._crv(s)
                    if cr & EN and (cr >> 16 & 3) > best_pl and self._ready(s, cr):
                        best, best_pl = s, cr >> 16 & 3
                if best < 0:
                    break
                guard += 1
                self._item(best)
        finally:
            self._busy = False

    def _item(self, s: int) -> None:
        """Move one peripheral-size data item on stream s (one granted request)."""
        st = self.streams[s]
        last = False
        if st.dir != DIR_M2M:
            for n in st.names:                             # acknowledge before the access: a peripheral that
                self.levels[n] = False                     # re-requests during it keeps its new request
                if self.lasts.get(n):
                    last = True
                    self.lasts[n] = False
        ps, ms = st.psize, st.msize
        try:
            if st.dir == DIR_M2P:
                if st.fifo_mode:
                    while len(st.fifo) < ps and st.mleft > 0:
                        k = min(ms, st.mleft)
                        st.fifo += self._rd(st.mptr, k).to_bytes(k, "little")
                        st.mptr = (st.mptr + (st.mstep if st.mstep else 0)) & 0xFFFFFFFF
                        st.mleft -= k
                    v = int.from_bytes(st.fifo[:ps], "little")
                    del st.fifo[:ps]
                else:
                    v = self._rd(st.mptr, ps)
                    st.mptr = (st.mptr + st.mstep) & 0xFFFFFFFF
                self._wr(st.pptr, ps, v)
                st.pptr = (st.pptr + st.pstep) & 0xFFFFFFFF
            else:                                          # P2M, M2M: source = PAR
                v = self._rd(st.pptr, ps)
                st.pptr = (st.pptr + st.pstep) & 0xFFFFFFFF
                if st.fifo_mode:
                    st.fifo += v.to_bytes(ps, "little")
                    if len(st.fifo) >= st.thresh:
                        self._flush(s, all_=False)
                else:
                    self._wr(st.mptr, ps, v)
                    st.mptr = (st.mptr + st.mstep) & 0xFFFFFFFF
            self.items += 1
            ndtr = (self._sreg(s, "NDTR") & 0xFFFF) - 1
            self.set(f"S{s}NDTR", ndtr)
            flags = 0
            if ndtr == st.n0 // 2:
                flags |= F_HT
            if ndtr == 0 or (last and st.pfc):
                if st.fifo and st.dir != DIR_M2P:
                    self._flush(s, all_=True)
                flags |= F_TC
                if ndtr == 0 and st.names:
                    self._notify_last(st.names)
                cr = self._crv(s)
                if st.circ:
                    if cr & DBM:
                        cr ^= CT
                        self._set_cr(s, cr)
                    self.set(f"S{s}NDTR", st.n0)
                    st.fifo = bytearray()
                    self._load_ptrs(s, cr)
                else:
                    self._set_cr(s, cr & ~EN)
            if st.fifo_mode:
                self._set_fs(s)
            if flags:
                self._set_flags(s, flags)
        except _DmaFault as e:
            self._fault(s, e)

    def _notify_last(self, names: tuple[str, ...]) -> None:
        """The DMA's "last transfer" signal to the requesting peripheral (used by the ADC's DDS bit)."""
        for blk in (getattr(self.chip, "peripherals", {}) or {}).values():
            fn = getattr(blk, "dma_transfer_complete", None)
            if fn is not None:
                fn(names)

    def _flush(self, s: int, all_: bool) -> None:
        """P2M/M2M FIFO -> memory in MSIZE units (all_: also the remainder, in the largest aligned units)."""
        st = self.streams[s]
        ms = st.msize
        f = st.fifo
        while len(f) >= ms:
            self._wr(st.mptr, ms, int.from_bytes(f[:ms], "little"))
            del f[:ms]
            st.mptr = (st.mptr + st.mstep) & 0xFFFFFFFF
        if all_ and f:
            # RM0090 10.3.12 "FIFO flush": fewer bytes than MSIZE left are still written with the MSIZE width
            # ("memory is written with an undesired value"); the missing bytes are written as 0 here.
            self._wr(st.mptr, ms, int.from_bytes(bytes(f) + bytes(ms - len(f)), "little"))
            del f[:]
            st.mptr = (st.mptr + st.mstep) & 0xFFFFFFFF


class _DmaFault(Exception):
    def __init__(self, addr: int, msg: str):
        super().__init__(msg)
        self.addr = addr
        self.msg = msg


def controllers(chip: Any) -> list[MMIOBlock]:
    """The chip's stm32.dma blocks (cached on the chip)."""
    c = getattr(chip, "_stm32_dmas", None)
    if c is None:
        c = [b for b in (getattr(chip, "peripherals", {}) or {}).values()
             if getattr(b, "model", None) == "stm32.dma" or getattr(b, "stm32_dma_api", False)]   # + stm32f1.dma
        try:
            chip._stm32_dmas = c
        except AttributeError:
            pass
    return c
