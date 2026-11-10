"""C2833x/C2823x McBSP (Multichannel Buffered Serial Port; SPRUI07 ch. 12).  McBSP-A at 0x5000, McBSP-B at 0x5040
(F2833x/F28234/F28235 only: the F28232 has McBSP-A alone, SPRS439 Table 5-2).  Registers (16-bit units, SPRUI07
Table 12-76 offsets): DRR2 0, DRR1 1, DXR2 2, DXR1 3, SPCR2 4, SPCR1 5, RCR2 6, RCR1 7, XCR2 8, XCR1 9, SRGR2 A,
SRGR1 B, MCR2 C, MCR1 D, RCERA-B E-F, XCERA-B 10-11, PCR 12, RCERC..XCERH 13-1E, MFFINT 23.  PCLKCR0.MCBSPxENCLK
gates the module (LSPCLK); PIE 6.5 MRINTA / 6.6 MXINTA (6.3/6.4 for B), looked up by name; DMA events MXEVTx/MREVTx
(PERINTSEL 14-17) through `dma_listeners`.

Model (bit level, on the pins):
* Sample rate generator (12.4): CLKSRG = LSPCLK (SCLKME = 0, CLKSM = 1) or the MCLKR / MCLKX pin (SCLKME = 1,
  CLKSM = 0 / 1; active edge per CLKRP / CLKXP, Table 12-6: every such pin edge is one CLKSRG cycle).  CLKG = CLKSRG /
  (CLKGDV + 1), high for CLKGDV/2 + 1 input cycles (odd divide: high p + 1, low p), starting high at the next CLKSRG
  edge after GRST = 1; one `warn` when CLKG > LSPCLK/2.  GRST = 0: CLKG and FSG low.  FSG (FRST = 1): pulses of FWID + 1
  CLKG cycles every FPER + 1 cycles, the first FPER + 1 cycles after FRST = 1.  With LSPCLK, edges are core-cycle
  exact (`sched.at_cycles`; one event per CLKG edge only while something uses them: a clock output pin, a frame in
  progress, frame syncs from FSG or a pin, FS-detect interrupts; otherwise the generator is skipped arithmetically).
  GSYNC is stored only (one `warn`).
* Clocks / frame syncs (PCR, Tables 12-4, 12-92): internal CLKX = CLKG (CLKXM = 1) or the MCLKX pin (CLKXP); CLKR =
  CLKX with DLB = 1 or in clock-stop mode, else CLKG (CLKRM = 1) or the MCLKR pin (CLKRP: 0 = sample on the falling
  pin edge).  FSX = FSG (FSXM = 1, FSGM = 1), a one-CLKG-cycle pulse at the next CLKG rising edge after each
  DXR-to-XSR copy that starts a frame (FSXM = 1, FSGM = 0; needs FRST = 1 like FSG, SPRUI07 12.4.2), or the MFSX pin (FSXP = 1: active low); FSR = FSX (DLB,
  clock stop), FSG (FSRM = 1) or the MFSR pin (FSRP).  Data is driven on the rising edge of internal CLKX, frame syncs
  and data are sampled on the falling edge of internal CLKX / CLKR; a frame starts on an inactive-to-active FS
  transition seen at a sampling edge.  Output pins: MCLKX/MCLKR toggle with CLKG while GRST = 1 (also while the
  transmitter / receiver is in reset), MFSX/MFSR show the frame sync (inactive while XRST / RRST = 0), MDX the data.
* Frames (RCR/XCR): one or two phases of (FRLEN + 1) words of 8/12/16/20/24/32 bits, words back to back, data delay
  0/1/2 bits (0: bit 0 is driven as soon as FS goes active).  MSB first; COMPAND = 1: 8-bit LSB first; 2/3: mu-law /
  A-law (G.711) compression of DXR1 / expansion to a left-justified 16-bit DRR1.
* Transmitter (12.3.6, 12.5.4-6): DXR1 write (DXR2 first for > 16 bits) -> XRDY = 0; copied to XSR as soon as XSR is
  empty (at once when idle, else when the shifting word ends) -> XRDY = 1 (XINT with XINTM = 0, MXEVT to the DMA),
  XEMPTY = 1.  A word that must start with nothing new in DXR resends the old DXR (XEMPTY = 0; zeros right after XRST).
  An FS pulse during a frame: XFIG = 0 -> XSYNCERR, the frame restarts (with the new DXR if one was written); XFIG = 1
  ignored.  XRST = 0: XRDY/XEMPTY/XSYNCERR = 0, MDX high impedance; XRST = 1: XRDY = 1 (event), XEMPTY = 0.
* Receiver (12.3.5, 12.5.2-3): word -> RBR -> DRR (RRDY = 1: RINT with RINTM = 0, MREVT to the DMA); while RRDY = 1 the
  next word waits in RBR and the third in RSR with RFULL = 1 (later words overwrite RSR).  Reading DRR1 clears RRDY
  and moves RBR -> DRR, RSR -> RBR (RFULL = 0).  RJUST: right-justified zero fill / sign extension, left-justified.
  Unexpected FS: RFIG = 0 -> RSYNCERR, the word in progress is discarded and a new frame starts; RFIG = 1 ignored.
* Interrupts (12.12): MRINT/MXINT pulse on RINT/XINT gated by MFFINT.RINTENA/XINTENA; RINTM/XINTM = 0 RRDY/XRDY,
  2 every frame sync (also in reset), 3 RSYNCERR/XSYNCERR (writing 1 to the bit also interrupts); 1 (multichannel
  block end) never fires (multichannel selection is not modelled).
* DLB = 1 (12.8.4): DR = internal DX, FSR = FSX, CLKR = CLKX; MDR is ignored, MCLKR/MFSR are inputs unless CLKRM/FSRM
  (then they show the internal receive clock / frame sync).
* Clock stop / SPI (CLKSTP = 2/3, 12.7): master (CLKXM = 1): every DXR-to-XSR copy starts a packet of one word (FSGM = 0
  -> MFSX active, FSXP, for the whole packet); MCLKX runs only for the WDLEN bits (idle level CLKXP; CLKSTP = 2: data
  changes on the leading edge, sampled on the trailing one; CLKSTP = 3: data half a cycle ahead, sampled on the
  leading edge); XDATDLY = RDATDLY = 1 as the manual requires.  Slave (CLKXM = 0): MCLKX/MFSX are inputs, bit 0 goes
  out on MDX as MFSX becomes active (data delay 0), the receiver samples MDR on the same clock.  A responder
  (`responder(tx, pcs, bits) -> rx`, e.g. chipem/devices/spi_flash.py with `spi: MCBSPA`) answers a master packet
  byte by byte (MSB first) instead of the MDR pin; `frame_end(False, False)` is called when MFSX deasserts.
Pins: MDXx, MDRx, MCLKXx, MCLKRx, MFSXx, MFSRx through the GPIO mux; the live direction/drive is reported to the GPIO
model (`signal_state`), outputs are driven edge by edge with PinBoard.drive_signal (fast clocks are summarised by the
event log's dense-pin policy), inputs are pin input listeners (so McBSP-A <-> McBSP-B or a second chip on a board
exchange words).  Words are recorded in `sent` / `received`.
Not modelled (one `warn` when used): multichannel selection (MCR RMCM/XMCM; all channels are enabled), GSYNC, the
reserved word lengths / data delay 3, FREE/SOFT (no emulation suspend), DXENA (DX turn-on delay), A-bis / AC97
specifics.  Between frames MDX keeps the last bit (silicon: high impedance).
"""
from __future__ import annotations

from collections import deque
from typing import Any, Callable

from ..registry import register
from ._util import C2000Block, build_regs, fmt_hz, gpio_resync, irq_line, lspclk_hz, listen_input, unlisten_input

# SPCR1
RRST, RRDY, RFULL, RSYNCERR, DXENA, DLB = 1, 2, 4, 8, 0x80, 0x8000
# SPCR2
XRST, XRDY, XEMPTY, XSYNCERR, GRST, FRST = 1, 2, 4, 8, 0x40, 0x80
# PCR
CLKRP, CLKXP, FSRP, FSXP, SCLKME, CLKRM, CLKXM, FSRM, FSXM = 1, 2, 4, 8, 0x80, 0x100, 0x200, 0x400, 0x800
# SRGR2
FSGM, CLKSM, GSYNC = 0x1000, 0x2000, 0x8000
# MFFINT
XINTENA, RINTENA = 1, 4
WDLEN = (8, 12, 16, 20, 24, 32, 32, 32)
_KEYS = ("DX", "DR", "CLKX", "CLKR", "FSX", "FSR")


# ---------------------------------------------------------------- G.711 companding (16-bit linear; Sun g711.c)
_UEND = (0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF)
_AEND = (0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF)


def _s16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _seg(v: int, table: tuple[int, ...]) -> int:
    for i, e in enumerate(table):
        if v <= e:
            return i
    return len(table)


def ulaw_compress(pcm: int) -> int:
    """16-bit linear (left-justified 14-bit) -> 8-bit mu-law (ITU-T G.711)."""
    v = _s16(pcm) >> 2
    mask = 0xFF
    if v < 0:
        v, mask = -v, 0x7F
    v = min(v, 8159) + (0x84 >> 2)
    seg = _seg(v, _UEND)
    if seg >= 8:
        return 0x7F ^ mask
    return ((seg << 4) | ((v >> (seg + 1)) & 0xF)) ^ mask


def ulaw_expand(u: int) -> int:
    """8-bit mu-law -> 16-bit linear (left-justified 14-bit), as a 16-bit register value."""
    u = ~u & 0xFF
    t = (((u & 0x0F) << 3) + 0x84) << ((u & 0x70) >> 4)
    return ((0x84 - t) if u & 0x80 else (t - 0x84)) & 0xFFFF


def alaw_compress(pcm: int) -> int:
    """16-bit linear (left-justified 13-bit) -> 8-bit A-law (ITU-T G.711)."""
    v = _s16(pcm) >> 3
    if v >= 0:
        mask = 0xD5
    else:
        mask, v = 0x55, -v - 1
    seg = _seg(v, _AEND)
    if seg >= 8:
        return 0x7F ^ mask
    a = seg << 4
    a |= (v >> 1) & 0xF if seg < 2 else (v >> seg) & 0xF
    return a ^ mask


def alaw_expand(a: int) -> int:
    """8-bit A-law -> 16-bit linear (left-justified 13-bit), as a 16-bit register value."""
    a = (a ^ 0x55) & 0xFF
    t = (a & 0x0F) << 4
    seg = (a & 0x70) >> 4
    if seg == 0:
        t += 8
    elif seg == 1:
        t += 0x108
    else:
        t = (t + 0x108) << (seg - 1)
    return (t if a & 0x80 else -t) & 0xFFFF


def _rev8(v: int) -> int:
    return int(f"{v & 0xFF:08b}"[::-1], 2)


@register("ti_c2000.mcbsp")
class McBSP(C2000Block):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        L = str(params.get("instance") or name[-1]).upper()
        self.letter = L
        cfg = {"on_write": self._cfg_write}
        ov = {
            "DRR2": {"ro_mask": 0xFFFF},
            "DRR1": {"ro_mask": 0xFFFF, "on_read": self._drr1_read},
            "DXR1": {"on_write": self._dxr1_write},
            "SPCR1": {"ro_mask": 0x0706, "on_write": self._spcr1_write},
            "SPCR2": {"ro_mask": 0xFC06, "on_write": self._spcr2_write},
            "RCR1": {"ro_mask": 0x801F, **cfg}, "XCR1": {"ro_mask": 0x801F, **cfg},
            "RCR2": cfg, "XCR2": cfg, "SRGR1": cfg, "SRGR2": cfg,
            "PCR": {"ro_mask": 0xF070, **cfg},
            "MCR1": {"ro_mask": 0xFC1E, **cfg}, "MCR2": {"ro_mask": 0xFC1C, **cfg},
            "MFFINT": {"ro_mask": 0xFFFA},
        }
        regs = build_regs((name, f"MCBSP{L}", "MCBSPA"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, regs, params=params, log=log)
        self.gate = params.get("gate") or f"MCBSP{L}ENCLK"
        self.sig = {"DX": f"MDX{L}", "DR": f"MDR{L}", "CLKX": f"MCLKX{L}", "CLKR": f"MCLKR{L}",
                    "FSX": f"MFSX{L}", "FSR": f"MFSR{L}"}
        self._key_of = {v: k for k, v in self.sig.items()}
        self.rint = params.get("rx_irq")
        self.xint = params.get("tx_irq")
        self.dma_listeners: list[Callable[[str, str], None]] = []
        self.responder: Callable[[int, int, int], int] | None = None
        self.frame_end: Callable[[bool, bool], None] | None = None
        self.sent: deque[int] = deque(maxlen=100_000)
        self.received: deque[int] = deque(maxlen=100_000)
        self._listen: set[str] = set()
        self._warned: set[str] = set()
        self._mux_cache: dict[str, bool] = {}
        self._ev = None
        self._init_state()

    # ------------------------------------------------------------------ state
    def _init_state(self) -> None:
        self._ev = None
        # sample rate generator
        self._t_srg = 0                 # core cycle where CLKG cycle 0 starts (internal LSPCLK source)
        self._in_cyc = 4                # core cycles per LSPCLK cycle
        self._Lc = 8                    # CLKG period, core cycles
        self._Hc = 4                    # CLKG high time, core cycles
        self._ext_count = 0             # CLKSRG edges seen (pin source)
        self._n = -1                    # current CLKG cycle index
        self._clkg = False
        self._fsg = False
        self._n_fsg0: int | None = None
        # transmitter
        self._dxr_new = False
        self._xsr = 0
        self._xsr_state = "empty"       # empty | loaded | shift
        self._tx_zero = True            # no DXR write since XRST: an underflow sends zeros
        self._tx_words: list[int] | None = None
        self._tx_wi = 0
        self._tx_bit = 0
        self._tx_len = 8
        self._tx_cur = 0
        self._tx_wait = 0
        self._tx_fs_prev = False
        self._fs_pend = False           # FSGM = 0: FS pulse at the next CLKG rising edge
        self._fsx_on = False            # internally generated FSX (FSGM = 0) active
        self._fsx_until = -1
        self._dx = "L"
        self._spi_win = (0, -1)         # clock-stop master: CLKG cycles in which MCLKX pulses
        self._resp: int | None = None   # SPI responder answer for the word in progress
        # receiver
        self._rx_words: list[int] | None = None
        self._rx_wi = 0
        self._rx_bit = 0
        self._rx_len = 8
        self._rx_wait = 0
        self._rsr = 0
        self._rx_fs_prev = False
        self._rbr: tuple[int, int] | None = None
        self._rsr_full: tuple[int, int] | None = None
        # pins
        self._out: dict[str, str | None] = {k: None for k in _KEYS}
        self._pin_prev: dict[str, str | None] = {k: None for k in _KEYS}

    def owned_signals(self) -> tuple[str, ...]:
        return tuple(self.sig.values())

    # ------------------------------------------------------------------ setup
    def attach(self, chip: Any) -> None:
        super().attach(chip)
        if self.rint is None:
            self.rint = irq_line(chip, f"MRINT{self.letter}", "6.5" if self.letter == "A" else "6.3")
        if self.xint is None:
            self.xint = irq_line(chip, f"MXINT{self.letter}", "6.6" if self.letter == "A" else "6.4")
        board = getattr(chip, "pins", None)
        if board is not None:
            for k in ("CLKX", "CLKR", "FSX"):
                for p in board.signal_pins(self.sig[k]):
                    key = (k, p.id)
                    if key not in self._listen:
                        self._listen.add(key)
                        listen_input(chip, p, self._make_listener(k))
        sc = self._sysctrl
        if sc is not None and hasattr(sc, "pclk_listeners"):
            sc.pclk_listeners.append(self._gate_changed)

    def reset(self) -> None:
        if self.chip is not None and self._ev is not None:
            self.chip.sched.cancel(self._ev)
        had = any(v is not None for v in self._out.values())
        self._init_state()
        self._warned.clear()
        self._mux_cache.clear()
        super().reset()
        if self.chip is not None and had:
            for k in _KEYS:
                self.chip.pins.release_signal(self.sig[k])
            for s in self.sig.values():
                gpio_resync(self.chip, s)

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=f"{self.name}: {msg}")

    # ------------------------------------------------------------------ register views
    def _spcr1(self) -> int:
        return self.get("SPCR1")

    def _spcr2(self) -> int:
        return self.get("SPCR2")

    def _pcr(self) -> int:
        return self.get("PCR")

    def _clkstp(self) -> int:
        c = (self._spcr1() >> 11) & 3
        return c if c >= 2 else 0

    def _rx_from_x(self) -> bool:
        """CLKR/FSR are the internal CLKX/FSX (digital loopback or clock-stop mode)."""
        return bool(self._spcr1() & DLB) or self._clkstp() != 0

    def _rx_clkg(self) -> bool:
        pcr = self._pcr()
        return bool(pcr & CLKXM) if self._rx_from_x() else bool(pcr & CLKRM)

    def _clk_on(self) -> bool:
        sc = self._sysctrl
        return sc is None or sc.clock_enabled(self.gate)

    def _srg_internal(self) -> bool:
        return not self._pcr() & SCLKME and bool(self.get("SRGR2") & CLKSM)

    def _srg_running(self) -> bool:
        return bool(self._spcr2() & GRST) and self._clk_on()

    def _frame(self, rx: bool) -> list[int]:
        c1, c2 = (self.get("RCR1"), self.get("RCR2")) if rx else (self.get("XCR1"), self.get("XCR2"))
        w1 = WDLEN[(c1 >> 5) & 7]
        if self._clkstp():
            return [w1]
        words = [w1] * (((c1 >> 8) & 0x7F) + 1)
        if c2 & 0x8000:
            words += [WDLEN[(c2 >> 5) & 7]] * (((c2 >> 8) & 0x7F) + 1)
        return words

    def _delay(self, rx: bool) -> int:
        d = self.get("RCR2" if rx else "XCR2") & 3
        if d == 3:
            self._warn_once("dly3", f"{'R' if rx else 'X'}DATDLY = 3 is reserved: 2-bit delay used")
            d = 2
        return d

    # ------------------------------------------------------------------ pins
    def _muxed(self, k: str) -> bool:
        m = self._mux_cache.get(k)
        if m is None:
            board = self.chip.pins
            sig = self.sig[k]
            m = self._mux_cache[k] = any(board.signal_matches(p.owner, sig) for p in board.signal_pins(sig))
        return m

    def _pin_level(self, k: str) -> str | None:
        board = self.chip.pins
        sig = self.sig[k]
        for p in board.signal_pins(sig):
            if board.signal_matches(p.owner, sig):
                return p.level()
        return None

    def _want_out(self) -> dict[str, bool]:
        pcr, s1, s2 = self._pcr(), self._spcr1(), self._spcr2()
        on = self._clk_on()
        return {"DX": bool(on and s2 & XRST), "DR": False, "CLKX": bool(on and pcr & CLKXM),
                "CLKR": bool(on and pcr & CLKRM), "FSX": bool(on and pcr & FSXM), "FSR": bool(on and pcr & FSRM)}

    def signal_state(self, sig: str) -> tuple[str, str | None] | None:
        k = self._key_of.get(sig)
        if k is None:
            return None
        lvl = self._out[k]
        return ("out", lvl) if lvl is not None else ("in", None)

    def _level_for(self, k: str) -> str:
        """Current output level of signal k (when it is an output)."""
        pcr = self._pcr()
        if k == "DX":
            return self._dx
        if k == "CLKX":
            return "H" if self._clkx_pin_internal(False) ^ bool(pcr & CLKXP) else "L"
        if k == "CLKR":
            return "H" if self._clkr_out_internal() ^ bool(pcr & CLKRP) else "L"
        if k == "FSX":
            act = bool(self._spcr2() & XRST) and self._fsx_internal()
            return "H" if act ^ bool(pcr & FSXP) else "L"
        if k == "FSR":
            act = bool(self._spcr1() & RRST) and self._fsg
            return "H" if act ^ bool(pcr & FSRP) else "L"
        return "L"

    def _apply_pins(self) -> None:
        if self.chip is None or self.chip.pins is None:
            return
        board = self.chip.pins
        changed = []
        for k, want in self._want_out().items():
            cur = self._out[k]
            if want:
                lvl = self._level_for(k)
                if cur is None:
                    self._out[k] = lvl
                    changed.append(k)
                elif cur != lvl:
                    self._out[k] = lvl
                    board.drive_signal(self.sig[k], lvl)
            elif cur is not None:
                self._out[k] = None
                board.release_signal(self.sig[k])
                changed.append(k)
        for k in changed:
            gpio_resync(self.chip, self.sig[k])
        for k in changed:
            if self._out[k] is not None:
                board.drive_signal(self.sig[k], self._out[k])
        for k in ("CLKX", "CLKR", "FSX"):
            if self._out[k] is None:
                self._pin_prev[k] = self._pin_level(k)

    def _drive(self, k: str, lvl: str) -> None:
        if self._out[k] is not None and self._out[k] != lvl:
            self._out[k] = lvl
            self.chip.pins.drive_signal(self.sig[k], lvl)

    def _set_dx(self, bit: int) -> None:
        lvl = "H" if bit else "L"
        self._dx = lvl
        if self._out["DX"] is not None and self._out["DX"] != lvl:
            self._out["DX"] = lvl
            self.chip.pins.drive_signal(self.sig["DX"], lvl)

    def mux_changed(self) -> None:
        """The GPIO mux moved a pin to / from one of our signals."""
        self._mux_cache.clear()
        self._apply_pins()
        self._kick()

    def _gate_changed(self, *a) -> None:
        self._apply_pins()
        self._kick()

    # ------------------------------------------------------------------ internal clock / frame-sync levels
    def _clkx_pin_internal(self, _unused: bool) -> bool:
        """Internal level shown on MCLKX (before CLKXP) when CLKXM = 1."""
        if not self._srg_running():
            return False
        if self._clkstp():
            c0, c1 = self._spi_win
            if not c0 <= self._n <= c1:
                return False
            if self._clkstp() == 2:
                return self._clkg
            return not self._clkg and self._n >= c0
        return self._clkg

    def _clkr_out_internal(self) -> bool:
        if self._rx_from_x():
            if self._pcr() & CLKXM:
                return self._clkx_pin_internal(False)
            return (self._pin_level("CLKX") == "H") ^ bool(self._pcr() & CLKXP)
        return self._clkg if self._srg_running() else False

    def _fsx_internal(self) -> bool:
        pcr = self._pcr()
        if pcr & FSXM:
            if self.get("SRGR2") & FSGM:
                return self._fsg
            return self._fsx_on
        lvl = self._pin_level("FSX")
        return lvl in ("H", "L") and ((lvl == "H") != bool(pcr & FSXP))

    def _fsr_internal(self) -> bool:
        if self._rx_from_x():
            return self._fsx_internal()
        pcr = self._pcr()
        if pcr & FSRM:
            return self._fsg
        lvl = self._pin_level("FSR")
        return lvl in ("H", "L") and ((lvl == "H") != bool(pcr & FSRP))

    # ------------------------------------------------------------------ sample rate generator
    def _srg_setup(self, origin: bool) -> None:
        """(Re)compute CLKG timing; `origin`: CLKG restarts (high) at the next LSPCLK edge."""
        if self.chip is None:
            return
        core = float(self.chip.clock.hz)
        lsp = lspclk_hz(self.chip) or core / 4
        ic = max(1, int(round(core / lsp)))
        div = (self.get("SRGR1") & 0xFF) + 1
        if div == 1:
            hc, lc = max(1, ic // 2), max(2, ic)
        else:
            hc, lc = ((div - 1) // 2 + 1) * ic, div * ic
        self._in_cyc, self._Hc, self._Lc = ic, hc, lc
        if self._srg_internal() and div == 1:
            self._warn_once("fast", f"CLKG = LSPCLK (CLKGDV = 0) exceeds LSPCLK/2 (SPRUI07 12.4.1.2: minimum CLKGDV "
                            "is 1 with CLKSM = 1)")
        if origin:
            now = self.chip.now_cycles()
            self._t_srg = -(-now // ic) * ic
            self._n = -1
            self._ext_count = 0

    def _cycle_now(self) -> int:
        now = self.chip.now_cycles()
        if now < self._t_srg:
            return -1
        return (now - self._t_srg) // self._Lc

    def _fsg_at(self, n: int) -> bool:
        n0 = self._n_fsg0
        if n0 is None or n < n0:
            return False
        per = (self.get("SRGR2") & 0xFFF) + 1
        return (n - n0) % per <= (self.get("SRGR1") >> 8) & 0xFF

    def _need_edges(self) -> bool:
        if not self._srg_running():
            return False
        pcr, s1, s2 = self._pcr(), self._spcr1(), self._spcr2()
        clkstp = self._clkstp()
        if self._out["CLKX"] is not None and not clkstp and self._muxed("CLKX"):
            return True                                     # free-running clock on a pin
        if self._out["CLKR"] is not None and not clkstp and self._muxed("CLKR"):
            return True
        fsg_used = bool(s2 & FRST) and (bool(pcr & FSXM and self.get("SRGR2") & FSGM)
                                        or bool(pcr & FSRM and not self._rx_from_x()))
        if fsg_used and (self._out["FSX"] is not None and self._muxed("FSX")
                         or self._out["FSR"] is not None and self._muxed("FSR")):
            return True
        if pcr & CLKXM:
            if s2 & XRST:
                if self._tx_words is not None or self._fs_pend or self._fsx_on or not pcr & FSXM or fsg_used:
                    return True
            elif (s2 >> 4) & 3 == 2 and (fsg_used or not pcr & FSXM):
                return True
        if self._rx_clkg():
            if s1 & RRST:
                if self._rx_words is not None:
                    return True
                if self._rx_from_x():
                    if not pcr & FSXM or fsg_used:
                        return True
                elif not pcr & FSRM or fsg_used:
                    return True
            elif (s1 >> 4) & 3 == 2:
                return True
        return False

    def _kick(self) -> None:
        """Start the CLKG edge stream if something needs it."""
        if self.chip is None or self._ev is not None or not self._srg_internal():
            return
        if not self._need_edges():
            return
        # the stream was idle: the frame syncs did not change meanwhile except through what restarts it now
        self._tx_fs_prev = self._fsx_internal()
        if self._rx_clkg():
            self._rx_fs_prev = self._fsr_internal()
        now = self.chip.now_cycles()
        n = self._cycle_now()
        if n < 0:
            self._ev = self.chip.sched.at_cycles(self._t_srg, self._clkg_edge, 0, False)
            return
        tr = self._t_srg + n * self._Lc
        tf = tr + self._Hc
        if self._n != n and tr >= now:
            self._ev = self.chip.sched.at_cycles(tr, self._clkg_edge, n, False)
        elif tf > now and self._n == n:
            self._ev = self.chip.sched.at_cycles(tf, self._clkg_edge, n, True)
        else:
            self._ev = self.chip.sched.at_cycles(self._t_srg + (n + 1) * self._Lc, self._clkg_edge, n + 1, False)

    def _clkg_edge(self, n: int, falling: bool) -> None:
        self._ev = None
        if not self._srg_running() or not self._srg_internal():
            return
        if not falling:
            ic = max(1, int(round(float(self.chip.clock.hz) / (lspclk_hz(self.chip) or 1))))
            if ic != self._in_cyc:                          # LOSPCP changed: keep the phase, new period
                t = self._t_srg + n * self._Lc
                self._srg_setup(False)
                self._t_srg = t - n * self._Lc
        self._edge(n, falling)
        if not self._need_edges():
            return
        if falling:
            self._ev = self.chip.sched.at_cycles(self._t_srg + (n + 1) * self._Lc, self._clkg_edge, n + 1, False)
        else:
            self._ev = self.chip.sched.at_cycles(self._t_srg + n * self._Lc + self._Hc, self._clkg_edge, n, True)

    def _srg_pin_edge(self) -> None:
        """One CLKSRG cycle from the MCLKR / MCLKX pin (SCLKME = 1)."""
        if not self._srg_running():
            return
        div = (self.get("SRGR1") & 0xFF) + 1
        c = self._ext_count
        self._ext_count += 1
        n, p = divmod(c, div)
        if p == 0:
            self._edge(n, False)
            if div == 1:
                self._edge(n, True)
        elif p == (div - 1) // 2 + 1:
            self._edge(n, True)

    def _edge(self, n: int, falling: bool) -> None:
        """A CLKG edge: frame-sync generation, transmitter / receiver clocked by CLKG, output pins."""
        pcr = self._pcr()
        if not falling:
            self._n = n
            self._clkg = True
            self._fsg = bool(self._spcr2() & FRST) and self._fsg_at(n)
            if self._fs_pend:
                self._fs_pend = False
                self._fsx_on = True
                self._fsx_until = n if not self._clkstp() else 1 << 62
            elif self._fsx_on and n > self._fsx_until:
                self._fsx_on = False
            if pcr & CLKXM:
                self._tx_edge(True)
            self._pins_update()
        else:
            self._clkg = False
            self._pins_update()
            if pcr & CLKXM:
                self._tx_edge(False)
            if self._rx_clkg():
                self._rx_sample()

    def _pins_update(self) -> None:
        out = self._out
        for k in ("CLKX", "CLKR", "FSX", "FSR"):
            if out[k] is not None:
                self._drive(k, self._level_for(k))

    # ------------------------------------------------------------------ pin input listeners
    def _make_listener(self, k: str):
        def cb(pin: Any, _k=k) -> None:
            self._pin_changed(_k, pin)
        return cb

    def _pin_changed(self, k: str, pin: Any) -> None:
        if self.clk_stopped:
            return                                          # STANDBY / HALT: no SYSCLKOUT
        if self.chip is None or not self.chip.pins.signal_matches(pin.owner, self.sig[k]):
            return
        if self._out[k] is not None:
            return                                          # our own output
        lvl = pin.level()
        prev = self._pin_prev[k]
        self._pin_prev[k] = lvl
        if lvl == prev or lvl not in ("H", "L") or prev not in ("H", "L") or not self._clk_on():
            return
        rising = lvl == "H"
        pcr = self._pcr()
        if k == "FSX":
            if pcr & FSXM:
                return
            if rising != bool(pcr & FSXP):
                self._tx_predrive()
            elif self._clkstp():                            # SPI slave deselected: no clock, end of packet
                self._tx_fs_prev = self._rx_fs_prev = False
                if self._tx_words is not None or self._rx_words is not None:
                    self._warn_once("spidesel", "SPI slave deselected (MFSX inactive) in the middle of a packet: "
                                    "the partial word is dropped")
                    self._tx_words = self._rx_words = None
                    if self._xsr_state == "shift":
                        self._xsr_state = "loaded"
            return
        if pcr & SCLKME and self._spcr2() & GRST:
            src = "CLKX" if self.get("SRGR2") & CLKSM else "CLKR"
            if src == k:
                pol = bool(pcr & CLKXP) if k == "CLKX" else not pcr & CLKRP
                if rising != pol:
                    self._srg_pin_edge()
        if k == "CLKX":
            if pcr & CLKXM:
                return
            internal_rise = rising != bool(pcr & CLKXP)
            if self._clkstp() == 3:
                internal_rise = not internal_rise
            if internal_rise:
                self._tx_edge(True)
            else:
                self._tx_edge(False)
                if self._rx_from_x():
                    self._rx_sample()
        elif k == "CLKR":
            if pcr & CLKRM or self._rx_from_x():
                return
            if rising == bool(pcr & CLKRP):                 # CLKRP = 0: sample on the falling edge
                self._rx_sample()

    # ------------------------------------------------------------------ interrupts / DMA events
    def _xint(self) -> None:
        if self.get("MFFINT") & XINTENA and self.xint is not None:
            self.pulse_irq(self.xint)

    def _rint(self) -> None:
        if self.get("MFFINT") & RINTENA and self.rint is not None:
            self.pulse_irq(self.rint)

    def _xrdy_rise(self) -> None:
        s2 = self._spcr2()
        if s2 & XRDY:
            return
        self.set("SPCR2", s2 | XRDY)
        for cb in self.dma_listeners:
            cb(self.letter, "X")
        if (self._spcr2() >> 4) & 3 == 0:
            self._xint()

    # ------------------------------------------------------------------ transmitter
    def _dxr_value(self) -> int:
        return (self.get("DXR2") << 16) | self.get("DXR1")

    def _encode(self, raw: int, n: int) -> int:
        comp = (self.get("XCR2") >> 3) & 3
        if comp:
            if n != 8:
                self._warn_once("xcomp", "XCOMPAND needs 8-bit words: companding applied to the low 8 bits")
            if comp == 1:
                return _rev8(raw)
            v = ulaw_compress(raw & 0xFFFF) if comp == 2 else alaw_compress(raw & 0xFFFF)
            return v & ((1 << n) - 1)
        return raw & ((1 << n) - 1)

    def _copy_dxr(self) -> None:
        self._xsr = self._dxr_value()
        self._xsr_state = "loaded"
        self._dxr_new = False
        self._tx_zero = False
        self.set("SPCR2", self._spcr2() | XEMPTY)
        self._xrdy_rise()
        if self._fs_by_copy() and self._tx_words is None and not self._fsx_on:
            self._fs_pend = True
            self._kick()

    def _fs_by_copy(self) -> bool:
        """FSXM = 1, FSGM = 0: a DXR-to-XSR copy generates the frame sync; the frame-sync logic must be enabled too
        (FRST = 1, SPRUI07 12.4.2 "in either case")."""
        return bool(self._pcr() & FSXM and not self.get("SRGR2") & FSGM and self._spcr2() & FRST)

    def _dxr1_write(self, blk, reg, old, new):
        self._store(reg, new)
        s2 = self._spcr2()
        self._dxr_new = True
        if not s2 & XRST:
            return None
        if s2 & XRDY:
            self.set("SPCR2", s2 & ~XRDY)
        elif self._xsr_state != "empty":
            self._warn_once("ovw", "DXR1 written while XRDY = 0: the previous word is overwritten (SPRUI07 12.5.4)")
        if self._xsr_state == "empty":
            self._copy_dxr()
        self._kick()
        return None

    def _tx_predrive(self) -> None:
        """Data delay 0: bit 0 of the next word goes out as soon as FSX becomes active."""
        s2 = self._spcr2()
        if not s2 & XRST or self._tx_words is not None or self.get("XCR2") & 3:
            return
        n = self._frame(False)[0]
        raw = self._xsr if self._xsr_state == "loaded" else (0 if self._tx_zero else self._dxr_value())
        self._set_dx((self._encode(raw, n) >> (n - 1)) & 1)

    def _tx_edge(self, rising: bool) -> None:
        s2 = self._spcr2()
        if not rising:
            fs = self._fsx_internal()
            edge = fs and not self._tx_fs_prev
            self._tx_fs_prev = fs
            if edge:
                self._tx_fs_detect()
            elif self._tx_words is not None and self._clkstp() and not self._pcr() & CLKXM \
                    and not self._tx_wait and self._tx_bit == self._tx_len - 1:
                # SPI slave: no clock edge follows the last bit of a packet; the word ends when it is sampled
                self._tx_word_end()
                self._tx_words = None
            return
        if not s2 & XRST:
            return
        if self._fsx_on and self._tx_words is None and not self.get("XCR2") & 3 and self._fsx_until == self._n:
            self._tx_predrive()                          # internal FS just went active, data delay 0
        if self._tx_words is None:
            return
        if self._tx_wait > 0:
            self._tx_wait -= 1
            if self._tx_wait == 0:
                self._tx_word_start()
            return
        self._tx_bit += 1
        if self._tx_bit < self._tx_len:
            self._set_dx((self._tx_cur >> (self._tx_len - 1 - self._tx_bit)) & 1)
            return
        self._tx_word_end()
        self._tx_wi += 1
        if self._tx_wi < len(self._tx_words):
            self._tx_word_start()
        else:
            self._tx_frame_done()

    def _tx_fs_detect(self) -> None:
        s2 = self._spcr2()
        xintm = (s2 >> 4) & 3
        if xintm == 2:
            self._xint()
        if not s2 & XRST:
            return
        if self._tx_words is not None:
            if self.get("XCR2") & 4:                        # XFIG
                return
            self.set("SPCR2", self._spcr2() | XSYNCERR)
            if xintm == 3:
                self._xint()
            if self._xsr_state == "shift":
                if self._dxr_new:
                    self._xsr_state = "empty"
                    self._copy_dxr()
                else:
                    self._xsr_state = "loaded"            # the same data is sent again
        self._tx_words = self._frame(False)
        self._tx_wi = 0
        d = self._delay(False)
        if d == 0:
            self._tx_word_start()
        else:
            self._tx_wait = d

    def _tx_word_start(self) -> None:
        n = self._tx_words[self._tx_wi]
        if self._xsr_state == "loaded":
            raw = self._xsr
        else:                                               # underflow: the old DXR again (zeros after XRST)
            raw = 0 if self._tx_zero else self._dxr_value()
            self.set("SPCR2", self._spcr2() & ~XEMPTY)
        self._xsr_state = "shift"
        self._tx_len = n
        self._tx_cur = self._encode(raw, n)
        self._tx_bit = 0
        self.sent.append(self._tx_cur)
        self._set_dx((self._tx_cur >> (n - 1)) & 1)
        if self._clkstp() and self._pcr() & CLKXM:
            self._spi_win = (self._n, self._n + n - 1)
            if self.responder is not None:
                self._resp = self._respond(self._tx_cur, n)

    def _respond(self, tx: int, n: int) -> int | None:
        if not (self._muxed("DX") and self._muxed("CLKX")):
            self._warn_once("nowire", "SPI master packet with MDX/MCLKX not muxed to a pin: the device does not see it")
            return None
        rx = 0
        done = 0
        while done < n:
            k = min(8, n - done)
            byte = (tx >> (n - done - k)) & ((1 << k) - 1)
            r = self.responder(byte, 0, k) & ((1 << k) - 1)
            rx = (rx << k) | r
            done += k
        return rx if self._muxed("DR") else 0

    def _tx_word_end(self) -> None:
        self._xsr_state = "empty"
        if self._dxr_new:
            self._copy_dxr()
        else:
            self.set("SPCR2", self._spcr2() & ~XEMPTY)

    def _tx_frame_done(self) -> None:
        self._tx_words = None
        if self._clkstp() and self._pcr() & CLKXM:
            self._fsx_on = False
            self._fsx_until = -1
            if self.frame_end is not None:
                self.frame_end(False, False)
            if self._xsr_state == "loaded" and self._fs_by_copy():
                self._fs_pend = True
        elif self._xsr_state == "loaded" and self._fs_by_copy() and not self._fsx_on:
            self._fs_pend = True

    # ------------------------------------------------------------------ receiver
    def _dr_bit(self) -> int:
        if self._resp is not None:
            return (self._resp >> (self._rx_len - 1 - self._rx_bit)) & 1
        if self._spcr1() & DLB:
            return 1 if self._dx == "H" else 0
        return 1 if self._pin_level("DR") == "H" else 0

    def _rx_sample(self) -> None:
        s1 = self._spcr1()
        if s1 & RRST and self._rx_words is not None:
            if self._rx_wait > 0:
                self._rx_wait -= 1
            if self._rx_wait == 0:
                self._rx_shift()
        fs = self._fsr_internal()
        edge = fs and not self._rx_fs_prev
        self._rx_fs_prev = fs
        if edge:
            self._rx_fs_detect()

    def _rx_shift(self) -> None:
        if self._rx_bit == 0:
            self._rx_len = self._rx_words[self._rx_wi]
            self._rsr = 0
        self._rsr = (self._rsr << 1) | self._dr_bit()
        self._rx_bit += 1
        if self._rx_bit < self._rx_len:
            return
        w, n = self._rsr, self._rx_len
        self._rx_bit = 0
        self._resp = None
        self._rx_word(w, n)
        self._rx_wi += 1
        if self._rx_wi >= len(self._rx_words):
            self._rx_words = None

    def _rx_fs_detect(self) -> None:
        s1 = self._spcr1()
        rintm = (s1 >> 4) & 3
        if rintm == 2:
            self._rint()
        if not s1 & RRST:
            return
        if self._rx_words is not None:
            if self.get("RCR2") & 4:                        # RFIG
                return
            self.set("SPCR1", self._spcr1() | RSYNCERR)
            if rintm == 3:
                self._rint()
        self._rx_words = self._frame(True)
        self._rx_wi = 0
        self._rx_bit = 0
        self._rx_wait = self._delay(True)
        if self._rx_wait == 0:
            self._rx_shift()

    def _rx_word(self, w: int, n: int) -> None:
        if (self.get("RCR2") >> 3) & 3 == 1:
            w = _rev8(w)
        self.received.append(w)
        if self._rbr is None and not self._spcr1() & RRDY:
            self._deliver(w, n)
        elif self._rbr is None:
            self._rbr = (w, n)
        else:
            self._rsr_full = (w, n)
            self.set("SPCR1", self._spcr1() | RFULL)

    def _deliver(self, w: int, n: int) -> None:
        comp = (self.get("RCR2") >> 3) & 3
        rj = (self.get("SPCR1") >> 13) & 3
        if comp >= 2:
            v = ulaw_expand(w & 0xFF) if comp == 2 else alaw_expand(w & 0xFF)
            self.set("DRR1", v)
        else:
            width = 16 if n <= 16 else 32
            if rj == 1 and w >> (n - 1) & 1:
                w |= ((1 << width) - 1) & ~((1 << n) - 1)
            elif rj >= 2:
                if rj == 3:
                    self._warn_once("rjust3", "RJUST = 3 is reserved: left justification used")
                w = (w << (width - n)) & ((1 << width) - 1)
            if n <= 16:
                self.set("DRR1", w)
            else:
                self.set("DRR2", w >> 16)
                self.set("DRR1", w & 0xFFFF)
        self.set("SPCR1", self._spcr1() | RRDY)
        for cb in self.dma_listeners:
            cb(self.letter, "R")
        if (self._spcr1() >> 4) & 3 == 0:
            self._rint()

    def _drr1_read(self, blk, reg):
        v = self.get("DRR1")
        s1 = self._spcr1()
        if not s1 & RRDY:
            return v
        self.set("SPCR1", s1 & ~RRDY)
        if self._rbr is not None:
            w, n = self._rbr
            self._rbr = self._rsr_full
            self._rsr_full = None
            self.set("SPCR1", self._spcr1() & ~RFULL)
            self._deliver(w, n)
        return v

    # ------------------------------------------------------------------ control registers
    def _spcr1_write(self, blk, reg, old, new):
        self._store(reg, new)
        if not new & RRST and old & RRST:
            self._rx_words = None
            self._rbr = self._rsr_full = None
            self._resp = None
            self.set("SPCR1", new & ~(RRDY | RFULL | RSYNCERR))
        if new & RSYNCERR and not old & RSYNCERR and (new >> 4) & 3 == 3 and new & RRST:
            self._rint()
        self._check_cfg()
        self._apply_pins()
        self._kick()
        return None

    def _spcr2_write(self, blk, reg, old, new):
        self._store(reg, new)
        # FREE/SOFT (0x300): stored only, like every C2000 model (no debugger halt, so no emulation suspend)
        if new & GRST and not old & GRST:
            self._srg_setup(True)
            if not self._srg_internal() and not self._pcr() & SCLKME:
                self._warn_once("srgsrc", "SCLKME = 0 with CLKSM = 0 is reserved: no CLKG")
            if self.get("SRGR2") & GSYNC:
                self._warn_once("gsync", "GSYNC (CLKG/FSG resynchronised to MFSR) is not modelled")
            hz = lspclk_hz(self.chip) / ((self.get("SRGR1") & 0xFF) + 1) if self._srg_internal() else 0
            if hz:
                self.emit("info", block=self.name, msg=f"{self.name}: CLKG {fmt_hz(hz)} (LSPCLK / "
                          f"{(self.get('SRGR1') & 0xFF) + 1})")
        elif old & GRST and not new & GRST:
            if self._ev is not None:
                self.chip.sched.cancel(self._ev)
                self._ev = None
            self._clkg = False
            self._fsg = False
            self._n_fsg0 = None
        if new & FRST and (not old & FRST or (new & GRST and not old & GRST)):
            per = (self.get("SRGR2") & 0xFFF) + 1
            self._n_fsg0 = max(self._cycle_now() if self._srg_internal() else self._n, 0) + per
        elif not new & FRST:
            self._n_fsg0 = None
            self._fsg = False
        if not new & XRST and old & XRST:
            self._tx_words = None
            self._tx_wait = 0
            self._xsr_state = "empty"
            self._fs_pend = False
            self._fsx_on = False
            self._resp = None
            self.set("SPCR2", self._spcr2() & ~(XRDY | XEMPTY | XSYNCERR))
        elif new & XRST and not old & XRST:
            self._xsr_state = "empty"
            self._tx_zero = True
            self.set("SPCR2", self._spcr2() & ~XEMPTY)
            if self._dxr_new:
                self._copy_dxr()
            else:
                self._xrdy_rise()
        if new & FRST and not old & FRST and new & XRST and self._xsr_state == "loaded" and self._tx_words is None \
                and self._fs_by_copy() and not self._fsx_on:
            self._fs_pend = True                            # a word loaded before FRST = 1 starts its frame now
        if new & XSYNCERR and not old & XSYNCERR and (new >> 4) & 3 == 3 and new & XRST:
            self._xint()
        self._check_cfg()
        self._apply_pins()
        self._kick()
        return None

    def _cfg_write(self, blk, reg, old, new):
        self._store(reg, new)
        if reg.name in ("SRGR1", "SRGR2", "PCR") and self._spcr2() & GRST and self._srg_internal():
            if reg.name != "PCR" or (old ^ new) & SCLKME:
                self._warn_once("srgcfg", f"{reg.name} changed while GRST = 1 (SPRUI07 12.10.2.3: change the "
                                "configuration in reset); CLKG restarts")
                if self._ev is not None:
                    self.chip.sched.cancel(self._ev)
                    self._ev = None
                self._srg_setup(True)
        self._check_cfg()
        self._apply_pins()
        self._kick()
        return None

    def _check_cfg(self) -> None:
        # a section in reset tracks the frame-sync level, so that it starts on the next inactive-to-active edge
        if not self._spcr2() & XRST:
            self._tx_fs_prev = self._fsx_internal()
        if not self._spcr1() & RRST:
            self._rx_fs_prev = self._fsr_internal()
        if self.get("MCR1") & 1 or self.get("MCR2") & 3:
            self._warn_once("mcm", "multichannel selection (MCR RMCM/XMCM) is not modelled: all channels enabled")
        for r in ("RCR1", "RCR2", "XCR1", "XCR2"):
            if (self.get(r) >> 5) & 7 >= 6:
                self._warn_once("wdlen", f"{r}: reserved word length: 32 bits used")
        if self._spcr1() & DXENA:
            self._warn_once("dxena", "DXENA (DX turn-on delay) is not modelled")
        if self._clkstp():
            if self.get("XCR2") & 0x8000 or self.get("XCR1") & 0x7F00 or self.get("RCR2") & 0x8000 \
                    or self.get("RCR1") & 0x7F00:
                self._warn_once("spiframe", "clock-stop mode needs single-phase one-word frames (SPRUI07 Table "
                                "12-14): one word per packet used")
            if self._pcr() & CLKXM and self.get("SRGR2") & FSGM:
                self._warn_once("spifsgm", "clock-stop master needs FSGM = 0 (SPRUI07 Table 12-16)")
