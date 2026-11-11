"""STM32F429/F439 LCD-TFT display controller (LTDC, RM0090 Rev 22 chapter 16).

Registers come from the chip's register table when it has an LTDC block, otherwise from chips/stm32f429_ltdc_regs.yaml
(svd2chip --peripherals LTDC of STM32F429.svd; the F429/F439 chips use the STM32F427.svd table for the rest of the
die, see chips/README.md).  Reserved bits (16.7) read 0 and ignore writes.  SVD quirk: LxCACR resets to 0x000000FF
(16.7.19), the SVD says 0.

Synchronous timing generator (16.4.1, 16.7.1-16.7.4): SSCR (HSW, VSH), BPCR (AHBP, AVBP), AWCR (AAW, AAH), TWCR
(TOTALW, TOTALH) are accumulated values minus 1.  A line is TOTALW + 1 pixel clocks, a frame TOTALH + 1 lines.  When
GCR.LTDCEN goes to 1 the generator starts at X/Y = 0/0 (first HSYNC pixel of the first VSYNC line); while it is 0 the
generator is held (CPSR reads X = TOTALW, Y = TOTALH, "the last pixel before the vertical synchronisation", CDSR keeps
its reset value 0x0000000F).  Pixel clock LCD_CLK = PLLSAI_R / PLLSAIDIVR from stm32.rcc_f42x (`ltdc_clk_hz()`, RM0090
6.3.24/6.3.25); without it (PLLSAI off, PLLSAIR < 2) the generator stalls (one `warn`).  Timing is lazy: no per-pixel
or per-line scheduler events.  CPSR (CXPOS/CYPOS) and CDSR (HSYNCS = X <= HSW, VSYNCS = Y <= VSH, HDES = AHBP < X <=
AAW, VDES = AVBP < Y <= AAH, polarity independent) are computed from the elapsed time when read.  Per frame there are
at most two events: the start of line LIPOS (line interrupt, LIF; no event when LIPOS > TOTALH) and the start of the
first line after the active area (line AAH + 1, the vertical blanking reload point of 16.7.6).  A change of the timing
registers, the pixel clock or LTDCEN re-bases the generator at the current position.

Shadow registers (16.4.1 "Reload shadow registers", 16.7.6): every layer register except LxCLUTWR is shadowed: reads
return the active value; SRCR.IMR copies the shadow values at once (IMR clears itself, no RRIF), SRCR.VBR at the start
of line AAH + 1 of the running frame (VBR then clears and RRIF is set; VBR cannot be cleared by software; while the
LTDC is disabled the reload waits).  A second write before the reload overrides the first.

Layers (16.4.2): window WHSTPOS..WHSPPOS x WVSTPOS..WVSPPOS (inclusive, timing coordinates), pixel formats ARGB8888,
RGB888, RGB565, ARGB1555, ARGB4444, L8, AL44, AL88 expanded by bit replication (chipem/periph/stm32/dma2d.py `decode`,
Table 90 byte order), CLUT (LxCLUTWR, 256 RGB entries; L8/AL88 index = L, AL44 index = L x 0x11 as 16.4.2 requires;
CLUTEN = 0: L is a grey level), colour keying (COLKEN: RGB equal to LxCKCR -> ARGB = 0, before blending), default
colour LxDCCR outside the window and for a disabled layer, constant alpha LxCACR, blending factors LxBFCR (BF1 100
constant alpha, 110 pixel x constant alpha; BF2 101 1 - constant alpha, 111 1 - pixel x constant alpha; reserved
codes: one `warn`, treated as the reset value): C = (BF1 x C + BF2 x Cs) / 255 with integer arithmetic (16.7.21 example:
240/255 x 128 + 15/255 x 48 = 123), layer 1 over the background colour BCCR, layer 2 over that (Figure 84).
Dithering (GCR.DEN) is stored but not applied: the model outputs the full 8 bits per channel.

Frame buffer fetch (16.7.22-16.7.24): CFBAR, pitch CFBP, line length CFBLL (= bytes + 3), CFBLNR lines.  Checked once
per frame at the end of the active area: an enabled layer whose window needs more bytes per line than CFBLL - 3 or more
lines than CFBLNR sets ISR.FUIF (FIFO underrun, 16.5; the missing pixels show the layer's default colour); a frame
buffer that the bus cannot read (outside the memory map, or the CCM data RAM, which the LTDC AHB master cannot reach)
sets TERRIF (the layer shows its default colour).  Memory bandwidth / FIFO timing underruns are not modelled.

Frames for board parts (duck-typed): a receiver (chipem/parts/lcd_panel.py) registers with `lcd_attach(rx)`; at the
end of each active area the LTDC calls `rx.lcd_frame(self, frame)` with an `LcdFrame` that holds a copy of the active
registers, the CLUTs and the frame buffer bytes taken at that moment and composes the RGB888 image lazily
(`frame.rgb()`).  `lcd_accepts(pin)` (pin muxed to LCD_CLK), `lcd_wiring(chip, ports)` (which LCD signals of the
receiver's nets are really carried by pins muxed to the LTDC) and `compose()` (the image of the current registers and
memory) complete the hook.

Pins: LCD_CLK, LCD_HSYNC, LCD_VSYNC and LCD_DE are not toggled per edge.  While the LTDC runs each pin muxed to one of
them gets one stored `pin` event with field="wave" per >= 100 ms (like chipem/periph/sdbus.py ClockWave): LCD_CLK at
the pixel clock (duty 50 %), HSYNC at the line rate (duty (HSW + 1) / (TOTALW + 1)), VSYNC at the frame rate (duty
(VSH + 1) / (TOTALH + 1)), DE pulses = active lines per second (duty = active pixels / total pixels).  The idle levels
of HSYNC/VSYNC/DE follow GCR.HSPOL/VSPOL/DEPOL (inactive level), data lines idle low (`signal_state`).

Interrupts (16.5, Table 91): IRQ "LTDC" (88) = LIF & LIE | RRIF & RRIE; IRQ "LTDC_ER" (89) = FUIF & FUIE | TERRIF &
TERRIE.  ICR (write 1 to clear) reads 0.  Clock gate RCC_APB2ENR.LTDCEN (one `warn` on access while gated); RCC_APB2RSTR
.LTDCRST / chip reset: registers, shadow values and CLUTs to reset, generator stopped.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ...bus import BusError
from ..base import pure_read
from ..registry import register
from ..sdbus import ClockWave
from ._util import GatedBlock, build_regs, gpio_resync, irq_number, rcc, svd_regs
from .dma2d import decode

LTDC_REGS = str(Path(__file__).resolve().parents[3] / "chips" / "stm32f429_ltdc_regs.yaml")

# GCR
LTDCEN, DEN = 1 << 0, 1 << 16
HSPOL, VSPOL, DEPOL, PCPOL = 1 << 31, 1 << 30, 1 << 29, 1 << 28
# SRCR
IMR, VBR = 1 << 0, 1 << 1
# IER / ISR / ICR
LIF, FUIF, TERRIF, RRIF = 1, 2, 4, 8
# LxCR
LEN, COLKEN, CLUTEN = 1 << 0, 1 << 1, 1 << 4

PF_NAMES = ("ARGB8888", "RGB888", "RGB565", "ARGB1555", "ARGB4444", "L8", "AL44", "AL88")
PF_BYTES = (4, 3, 2, 2, 2, 1, 1, 2)
LAYER_REGS = ("CR", "WHPCR", "WVPCR", "CKCR", "PFCR", "CACR", "DCCR", "BFCR", "CFBAR", "CFBLR", "CFBLNR")
_LAYER_RO = {"CR": 0xFFFFFFEC, "WHPCR": 0xF000F000, "WVPCR": 0xF800F800, "CKCR": 0xFF000000, "PFCR": 0xFFFFFFF8,
             "CACR": 0xFFFFFF00, "DCCR": 0, "BFCR": 0xFFFFF8F8, "CFBAR": 0, "CFBLR": 0xE000E000,
             "CFBLNR": 0xFFFFF800}
_TIMING = ("SSCR", "BPCR", "AWCR", "TWCR")


class _Wave(ClockWave):
    """ClockWave with a duty cycle and an idle level (LCD sync / data enable summaries)."""

    def __init__(self, block: Any, signal: str):
        super().__init__(block, signal)
        self.duty = 50.0
        self.idle = "L"

    def flush(self, stop: bool = False) -> None:
        chip = self.chip
        if chip is None:
            return
        self._accumulate()
        if stop:
            self._t0 = None
            if self._ev is not None:
                chip.sched.cancel(self._ev)
                self._ev = None
        n = int(self._edges)
        if n and self._since is not None:
            board = chip.pins
            for p in board.signal_pins(self.signal):
                if board.signal_matches(p.owner, self.signal):
                    self.block.emit("pin", pin=p.id, field="wave", old=None, new=self.idle, edges=n,
                                    since_ns=self._since, freq_hz=self.hz, duty=round(self.duty, 3))
        self._edges = 0.0
        self._since = None if stop else self._now()


class LcdFrame:
    """One displayed frame: copies of the active LTDC state and the frame buffer bytes; `rgb()` composes the
    active area as RGB888 bytes (row-major, 3 bytes per pixel) on first use."""

    def __init__(self, number: int, t_ns: float, geom: dict, regs: dict, cluts: list, data: list, bad: list):
        self.number = number
        self.t_ns = t_ns
        self.geom = geom
        self.regs = regs
        self.cluts = cluts
        self.data = data            # per layer: (bytes, base offset) or None
        self.bad = bad              # per layer: reason the layer shows its default colour, or ""
        self.width = max(0, geom["aaw"] - geom["ahbp"])
        self.height = max(0, geom["aah"] - geom["avbp"])
        self._rgb: bytes | None = None
        self.warnings: list[str] = []

    def rgb(self) -> bytes:
        if self._rgb is None:
            self._rgb = _compose(self)
        return self._rgb

    def pixel(self, x: int, y: int) -> tuple[int, int, int]:
        d = self.rgb()
        k = 3 * (y * self.width + x)
        return d[k], d[k + 1], d[k + 2]


def _layer_pixels(fr: LcdFrame, li: int, y: int) -> list[int] | None:
    """ARGB8888 of layer li (0/1) for active-area line y (timing coordinate), full active width; None = the layer
    contributes nothing (disabled with a transparent default colour and reset blending factors)."""
    g, r = fr.geom, fr.regs[li]
    x0, x1 = g["ahbp"] + 1, g["aaw"]
    w = x1 - x0 + 1
    dc = r["DCCR"]
    on = bool(r["CR"] & LEN)
    if not on and dc >> 24 == 0 and (r["BFCR"] >> 8) & 7 == 6:
        return None
    line = [dc] * w
    if not on or fr.bad[li] == "terr":
        return line
    whst, whsp = r["WHPCR"] & 0xFFF, (r["WHPCR"] >> 16) & 0xFFF
    wvst, wvsp = r["WVPCR"] & 0x7FF, (r["WVPCR"] >> 16) & 0x7FF
    if not wvst <= y <= wvsp:
        return line
    sx, ex = max(whst, x0), min(whsp, x1)
    if ex < sx:
        return line
    pf = r["PFCR"] & 7
    bpp = PF_BYTES[pf]
    j = y - wvst
    if j >= (r["CFBLNR"] & 0x7FF):
        return line                                     # underrun: no more lines in the frame buffer
    avail = max(0, ((r["CFBLR"] & 0x1FFF) - 3) // bpp)  # pixels per frame buffer line
    first = sx - whst                                   # pixel index within the frame buffer line
    n = min(ex - sx + 1, max(0, avail - first))
    if n <= 0:
        return line
    buf = fr.data[li]
    off = j * ((r["CFBLR"] >> 16) & 0x1FFF) + first * bpp
    raw = buf[off:off + n * bpp]
    if len(raw) < n * bpp:
        return line
    if pf <= 4:
        px = decode(pf, raw, n)
    else:
        clut = fr.cluts[li] if r["CR"] & CLUTEN else None
        if pf == 5:
            px = [0xFF000000 | clut[b] for b in raw] if clut else [0xFF000000 | b * 0x010101 for b in raw]
        elif pf == 6:
            if clut:
                px = [((b >> 4) * 17) << 24 | clut[(b & 15) * 0x11] for b in raw]
            else:
                px = [((b >> 4) * 17) << 24 | (b & 15) * 0x111111 for b in raw]
        else:
            px = [raw[2 * k + 1] << 24 | (clut[raw[2 * k]] if clut else raw[2 * k] * 0x010101) for k in range(n)]
    if r["CR"] & COLKEN:
        key = r["CKCR"] & 0xFFFFFF
        px = [0 if p & 0xFFFFFF == key else p for p in px]
    line[sx - x0:sx - x0 + n] = px
    return line


def _compose(fr: LcdFrame) -> bytes:
    g = fr.geom
    w, h = fr.width, fr.height
    out = bytearray(3 * w * h)
    if w <= 0 or h <= 0:
        return bytes(out)
    bg = fr.regs[2]["BCCR"] & 0xFFFFFF
    bgr, bgg, bgb = bg >> 16, (bg >> 8) & 255, bg & 255
    fac = []
    for li in (0, 1):
        r = fr.regs[li]
        bf1, bf2 = (r["BFCR"] >> 8) & 7, r["BFCR"] & 7
        if bf1 not in (4, 6):
            fr.warnings.append(f"L{li + 1}BFCR.BF1 = {bf1:03b} is reserved: treated as 110")
            bf1 = 6
        if bf2 not in (5, 7):
            fr.warnings.append(f"L{li + 1}BFCR.BF2 = {bf2:03b} is reserved: treated as 111")
            bf2 = 7
        fac.append((bf1 == 6, bf2 == 7, r["CACR"] & 255))
    k = 0
    for y in range(g["avbp"] + 1, g["aah"] + 1):
        rr, gg, bb = [bgr] * w, [bgg] * w, [bgb] * w
        for li in (0, 1):
            px = _layer_pixels(fr, li, y)
            if px is None:
                continue
            pa1, pa2, ca = fac[li]
            for i, p in enumerate(px):
                a = (p >> 24) * ca // 255 if pa1 else ca
                b = 255 - ((p >> 24) * ca // 255 if pa2 else ca)
                rr[i] = (((p >> 16) & 255) * a + rr[i] * b) // 255
                gg[i] = (((p >> 8) & 255) * a + gg[i] * b) // 255
                bb[i] = ((p & 255) * a + bb[i] * b) // 255
        row = bytearray(3 * w)
        row[0::3], row[1::3], row[2::3] = bytes(rr), bytes(gg), bytes(bb)
        out[k:k + 3 * w] = row
        k += 3 * w
    return bytes(out)


@register("stm32.ltdc")
class LTDC(GatedBlock):
    model = "stm32.ltdc"

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.gate = params.get("gate", "LTDC")
        ov: dict[str, dict] = {
            "SSCR": {"ro_mask": 0xF000F800, "on_write": self._timing_write},
            "BPCR": {"ro_mask": 0xF000F800, "on_write": self._timing_write},
            "AWCR": {"ro_mask": 0xF000F800, "on_write": self._timing_write},
            "TWCR": {"ro_mask": 0xF000F800, "on_write": self._timing_write},
            "GCR": {"ro_mask": 0x0FFEFFFE, "reset": 0x00002220, "on_write": self._gcr_write},
            "SRCR": {"ro_mask": 0xFFFFFFFC, "on_write": self._srcr_write},
            "BCCR": {"ro_mask": 0xFF000000},
            "IER": {"ro_mask": 0xFFFFFFF0, "on_write": self._ier_write},
            "ISR": {"ro_mask": 0xFFFFFFFF},
            "ICR": {"ro_mask": 0xFFFFFFF0, "on_write": self._icr_write, "on_read": pure_read(lambda b, r: 0)},
            "LIPCR": {"ro_mask": 0xFFFFF800, "on_write": self._lipcr_write},
            "CPSR": {"ro_mask": 0xFFFFFFFF, "on_read": self._cpsr_read},
            "CDSR": {"ro_mask": 0xFFFFFFFF, "reset": 0x0000000F, "on_read": self._cdsr_read},
        }
        for x in (1, 2):
            for rn, ro in _LAYER_RO.items():
                ov[f"L{x}{rn}"] = {"ro_mask": ro, "on_write": self._shadow_write}
            ov[f"L{x}CACR"]["reset"] = 0xFF                 # RM0090 16.7.19 (the SVD says 0)
            ov[f"L{x}CLUTWR"] = {"on_write": self._clut_write(x - 1), "on_read": pure_read(lambda b, r: 0)}
        rf = params.get("regs_file")
        if svd_regs((name, "LTDC"), rf) is None:
            rf = LTDC_REGS
        r = build_regs((name, "LTDC"), size, ov, rf)
        super().__init__(name, base, size, r, log=log, params=params)
        self._shadow: dict[str, int] = {}
        self.cluts = [[0] * 256, [0] * 256]
        self.receivers: list[Any] = []                      # board parts (lcd_attach), kept across resets
        self.capture = bool(params.get("capture", False))   # keep `last_frame` without a receiver (tests)
        self.last_frame: LcdFrame | None = None
        self.frames = 0                                     # frames displayed since the last enable
        self._irq: int | None = None
        self._irq_er: int | None = None
        self._lvl = [False, False]
        self._running = False
        self._t0 = 0.0                                      # time of pixel 0 of the current timing origin
        self._pos0 = 0                                      # pixel-clock index at _t0 (within a frame)
        self._period = 0.0                                  # pixel clock period (ns), 0 = stalled
        self._ev_line = None
        self._ev_vb = None
        self._warned: set[str] = set()
        self._no_access: list[tuple[int, int]] = []
        self.waves = {s: _Wave(self, s) for s in ("LCD_CLK", "LCD_HSYNC", "LCD_VSYNC", "LCD_DE")}

    # ------------------------------------------------------------------ setup
    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irq = irq_number(chip, "LTDC", "LCD_TFT")
        self._irq_er = irq_number(chip, "LTDC_ER", "LCD_TFT_1")
        r = rcc(chip)
        if r is not None and hasattr(r, "listeners"):
            r.listeners.append(self._clock_changed)
        self._no_access = []
        for m in getattr(getattr(chip, "desc", None), "memory", None) or []:
            if str(getattr(m, "name", "")).upper().startswith("CCM"):
                self._no_access.append((m.base, m.base + m.size))

    def reset(self) -> None:
        self._stop()
        self._shadow = {}
        self.cluts = [[0] * 256, [0] * 256]
        self.frames = 0
        self.last_frame = None
        self._warned.clear()
        super().reset()
        self._update_irq()
        if self.chip is not None:
            for s in self.waves:
                gpio_resync(self.chip, s)

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
            self.emit("warn", block=self.name, msg=msg)

    # ------------------------------------------------------------------ geometry / time
    def geometry(self) -> dict:
        s, b, a, t = (self.get(n) for n in _TIMING)
        return {"hsw": (s >> 16) & 0xFFF, "vsh": s & 0x7FF, "ahbp": (b >> 16) & 0xFFF, "avbp": b & 0x7FF,
                "aaw": (a >> 16) & 0xFFF, "aah": a & 0x7FF, "totalw": (t >> 16) & 0xFFF, "totalh": t & 0x7FF}

    def _dims(self) -> tuple[int, int]:
        g = self.geometry()
        return g["totalw"] + 1, g["totalh"] + 1

    def pixel_clock_hz(self) -> float:
        r = rcc(self.chip) if self.chip is not None else None
        f = getattr(r, "ltdc_clk_hz", None)
        if f is not None:
            return float(f())
        return float(self.params.get("pixel_clock_hz", 0.0))

    def frame_hz(self) -> float:
        w, h = self._dims()
        hz = self.pixel_clock_hz()
        return hz / (w * h) if hz else 0.0

    def _clocks(self, t: float | None = None) -> int:
        """Pixel clocks since the start of the current frame-0 origin (absolute index, not wrapped)."""
        if not self._running or not self._period:
            return self._pos0
        t = self.chip.now_ns() if t is None else t
        return self._pos0 + int((t - self._t0) / self._period)

    def position(self) -> tuple[int, int]:
        """(X, Y) of the timing generator now."""
        g = self.geometry()
        if not self._running:
            return g["totalw"], g["totalh"]
        w, h = g["totalw"] + 1, g["totalh"] + 1
        n = self._clocks() % (w * h)
        return n % w, n // w

    def _rebase(self) -> None:
        """Restart the time origin at the current position (clock / timing change)."""
        if not self._running:
            return
        now = self.chip.now_ns()
        w, h = self._dims()
        n = self._clocks(now) % (w * h)
        hz = self.pixel_clock_hz()
        self._period = 1e9 / hz if hz > 0 else 0.0
        if not self._period:
            self._warn_once("noclk", f"{self.name}: no LCD clock (PLLSAI off or PLLSAIR < 2, RM0090 6.3.24): the "
                            "timing generator stalls")
        self._t0, self._pos0 = now, n
        self._schedule()
        self._waves_update()

    def _time_of(self, line: int) -> float | None:
        """Next time (> now) the generator reaches X = 0 of `line`."""
        if not self._period:
            return None
        w, h = self._dims()
        if line > h - 1:
            return None
        frame = w * h
        target = line * w
        now = self.chip.now_ns()
        cur = self._clocks(now)
        base = cur - cur % frame
        n = base + target
        while self._t0 + (n - self._pos0) * self._period <= now:
            n += frame
        return self._t0 + (n - self._pos0) * self._period

    def _schedule(self) -> None:
        self._cancel()
        if not self._running or not self._period:
            return
        g = self.geometry()
        t = self._time_of(self.get("LIPCR") & 0x7FF)
        if t is not None:
            self._ev_line = self.chip.sched.at_ns(t, self._line_event)
        t = self._time_of((g["aah"] + 1) % (g["totalh"] + 1))
        if t is not None:
            self._ev_vb = self.chip.sched.at_ns(t, self._blank_event)

    def _cancel(self) -> None:
        if self.chip is not None:
            for ev in (self._ev_line, self._ev_vb):
                if ev is not None:
                    self.chip.sched.cancel(ev)
        self._ev_line = self._ev_vb = None

    def _start(self) -> None:
        self._running = True
        self._pos0 = 0
        self._t0 = self.chip.now_ns()
        self.frames = 0
        hz = self.pixel_clock_hz()
        self._period = 1e9 / hz if hz > 0 else 0.0
        g = self.geometry()
        if not (g["hsw"] <= g["ahbp"] < g["aaw"] <= g["totalw"] and g["vsh"] <= g["avbp"] < g["aah"] <= g["totalh"]):
            self._warn_once("geom", f"{self.name}: timing registers are not increasing (HSW {g['hsw']} <= AHBP "
                            f"{g['ahbp']} < AAW {g['aaw']} <= TOTALW {g['totalw']}, VSH {g['vsh']} <= AVBP "
                            f"{g['avbp']} < AAH {g['aah']} <= TOTALH {g['totalh']}; RM0090 16.4.1)")
        if g["aaw"] - g["ahbp"] > 1024 or g["aah"] - g["avbp"] > 768:
            self._warn_once("xga", f"{self.name}: active area {g['aaw'] - g['ahbp']}x{g['aah'] - g['avbp']} exceeds "
                            "the 1024x768 maximum (RM0090 16.2)")
        if not self._period:
            self._warn_once("noclk", f"{self.name}: LTDC enabled without an LCD clock (PLLSAI off or PLLSAIR < 2, "
                            "RM0090 6.3.24): the timing generator stalls")
        self.emit("info", block=self.name, msg=f"LTDC on: {g['aaw'] - g['ahbp']}x{g['aah'] - g['avbp']} active, "
                  f"{g['totalw'] + 1}x{g['totalh'] + 1} total, pixel clock {hz / 1e6:.3f} MHz, "
                  f"{self.frame_hz():.2f} Hz frame rate")
        self._schedule()
        self._waves_update()

    def _stop(self) -> None:
        self._cancel()
        if self._running:
            for wv in self.waves.values():
                wv.set(0.0, False)
        for wv in self.waves.values():
            wv.reset()
        self._running = False
        self._pos0 = 0

    def _clock_changed(self) -> None:
        if self._running:
            hz = self.pixel_clock_hz()
            if (1e9 / hz if hz > 0 else 0.0) != self._period:
                self._rebase()

    # ------------------------------------------------------------------ waves / pins
    def _waves_update(self) -> None:
        hz = self.pixel_clock_hz() if self._running and self._period else 0.0
        g = self.geometry()
        w, h = g["totalw"] + 1, g["totalh"] + 1
        gcr = self.get("GCR")
        line_hz, frame_hz = hz / w, hz / (w * h)
        active_lines = max(0, g["aah"] - g["avbp"])
        spec = {"LCD_CLK": (hz, 50.0, "H" if gcr & PCPOL else "L"),
                "LCD_HSYNC": (line_hz, 100.0 * (g["hsw"] + 1) / w, "L" if gcr & HSPOL else "H"),
                "LCD_VSYNC": (frame_hz, 100.0 * (g["vsh"] + 1) / h, "L" if gcr & VSPOL else "H"),
                "LCD_DE": (frame_hz * active_lines,
                           100.0 * max(0, g["aaw"] - g["ahbp"]) * active_lines / (w * h), "L" if gcr & DEPOL else "H")}
        for s, (f, duty, idle) in spec.items():
            wv = self.waves[s]
            if f != wv.hz:
                wv.set(0.0, False)
                wv.duty, wv.idle = duty, idle
                wv.set(f, False)
            else:
                wv.duty, wv.idle = duty, idle

    def signal_state(self, sig: str) -> tuple[str, str | None] | None:
        if not sig.startswith("LCD_"):
            return None
        gcr = self.get("GCR")
        if sig == "LCD_HSYNC":
            return "out", "L" if gcr & HSPOL else "H"
        if sig == "LCD_VSYNC":
            return "out", "L" if gcr & VSPOL else "H"
        if sig == "LCD_DE":
            return "out", "L" if gcr & DEPOL else "H"
        if sig == "LCD_CLK":
            return "out", "H" if gcr & PCPOL else "L"
        return "out", "L"

    # ------------------------------------------------------------------ registers
    def _timing_write(self, blk, reg, old, new):
        if old != new and self._running:
            self._store(reg, new)
            self._rebase()
        return None

    def _gcr_write(self, blk, reg, old, new):
        self._store(reg, new)
        if (old ^ new) & LTDCEN:
            if new & LTDCEN:
                self._start()
            else:
                self.emit("info", block=self.name, msg=f"LTDC off after {self.frames} frame(s)")
                self._stop()
        elif self._running and (old ^ new) & (HSPOL | VSPOL | DEPOL | PCPOL):
            self._waves_update()
        if (old ^ new) & (HSPOL | VSPOL | DEPOL | PCPOL) and self.chip is not None:
            for s in self.waves:
                gpio_resync(self.chip, s)
        return None

    def _shadow_write(self, blk, reg, old, new):
        self._shadow[reg.name] = new
        return old                                   # the active register keeps its value until a reload

    def _reload(self) -> None:
        for n, v in self._shadow.items():
            self.set(n, v)
        self._shadow = {}

    def _srcr_write(self, blk, reg, old, new):
        if new & IMR:
            self._reload()
        return (new | old) & VBR                      # IMR cleared by hardware; VBR only by the reload

    def _clut_write(self, li: int):
        def wr(blk, reg, old, new):
            if self._running and self.get(f"L{li + 1}CR") & LEN and self._in_active():
                self._warn_once(f"clut{li}", f"{self.name}: L{li + 1}CLUTWR written during the active display "
                                "period of an enabled layer (RM0090 16.7.25: only during blanking or with the layer "
                                "disabled)")
            self.cluts[li][new >> 24] = new & 0xFFFFFF
            return 0
        return wr

    def _in_active(self) -> bool:
        x, y = self.position()
        g = self.geometry()
        return g["avbp"] < y <= g["aah"] and g["ahbp"] < x <= g["aaw"]

    def _ier_write(self, blk, reg, old, new):
        self._store(reg, new)
        self._update_irq()
        return None

    def _icr_write(self, blk, reg, old, new):
        if new & 0xF:
            self.set("ISR", self.get("ISR") & ~new & 0xF)
            self._update_irq()
        return 0

    def _lipcr_write(self, blk, reg, old, new):
        if old != new and self._running:
            self._store(reg, new)
            self._schedule()
        return None

    @pure_read
    def _cpsr_read(self, blk, reg):
        x, y = self.position()
        return (x & 0xFFFF) << 16 | (y & 0xFFFF)

    @pure_read
    def _cdsr_read(self, blk, reg):
        if not self._running:
            return 0x0000000F
        x, y = self.position()
        g = self.geometry()
        return ((x <= g["hsw"]) << 3 | (y <= g["vsh"]) << 2 | (g["ahbp"] < x <= g["aaw"]) << 1 |
                (g["avbp"] < y <= g["aah"]))

    # ------------------------------------------------------------------ flags / IRQ
    def _flag(self, bits: int) -> None:
        self.set("ISR", self.get("ISR") | bits)
        self._update_irq()

    def _update_irq(self) -> None:
        if self.chip is None:
            return
        act = self.get("ISR") & self.get("IER")
        for k, (n, lvl) in enumerate(((self._irq, bool(act & (LIF | RRIF))), (self._irq_er, bool(act & (FUIF | TERRIF))))):
            if lvl != self._lvl[k] and n is not None:
                self.irq(n, lvl)
            self._lvl[k] = lvl

    # ------------------------------------------------------------------ frame events
    def _line_event(self) -> None:
        self._ev_line = None
        self._flag(LIF)
        t = self._time_of(self.get("LIPCR") & 0x7FF)
        if t is not None:
            self._ev_line = self.chip.sched.at_ns(t, self._line_event)

    def _blank_event(self) -> None:
        """Start of the first line after the active area: frame done (fetch checks, receivers), VBR reload."""
        self._ev_vb = None
        self.frames += 1
        fr = self._snapshot(self.receivers or self.capture)
        for rx in list(self.receivers):
            fn = getattr(rx, "lcd_frame", None)
            if fn is not None:
                fn(self, fr)
        if self.get("SRCR") & VBR:
            self._reload()
            self.set("SRCR", self.get("SRCR") & ~VBR)
            self._flag(RRIF)
        g = self.geometry()
        t = self._time_of((g["aah"] + 1) % (g["totalh"] + 1))
        if t is not None:
            self._ev_vb = self.chip.sched.at_ns(t, self._blank_event)

    def _layer_regs(self, li: int) -> dict:
        r = {rn: self.get(f"L{li + 1}{rn}") for rn in LAYER_REGS}
        bf1, bf2 = (r["BFCR"] >> 8) & 7, r["BFCR"] & 7
        if bf1 not in (4, 6) or bf2 not in (5, 7):
            self._warn_once(f"bf{li}", f"{self.name}: L{li + 1}BFCR = 0x{r['BFCR']:03X} selects a reserved blending "
                            "factor (RM0090 16.7.21): treated as the reset value (BF1 110, BF2 111)")
        return r

    def _fetch_plan(self, li: int, r: dict, g: dict) -> tuple[int, int, str] | None:
        """(address, bytes, underrun?) of the frame buffer area layer li reads this frame, None when nothing."""
        if not r["CR"] & LEN:
            return None
        whst, whsp = r["WHPCR"] & 0xFFF, (r["WHPCR"] >> 16) & 0xFFF
        wvst, wvsp = r["WVPCR"] & 0x7FF, (r["WVPCR"] >> 16) & 0x7FF
        sx, ex = max(whst, g["ahbp"] + 1), min(whsp, g["aaw"])
        sy, ey = max(wvst, g["avbp"] + 1), min(wvsp, g["aah"])
        if ex < sx or ey < sy:
            return None
        bpp = PF_BYTES[r["PFCR"] & 7]
        ll = (r["CFBLR"] & 0x1FFF) - 3
        pitch = (r["CFBLR"] >> 16) & 0x1FFF
        nl = r["CFBLNR"] & 0x7FF
        need_px = ex - whst + 1
        need_lines = ey - wvst + 1
        under = "" if ll >= need_px * bpp and nl >= need_lines else "underrun"
        lines = min(nl, need_lines)
        if lines <= 0 or ll <= 0:
            return None
        return r["CFBAR"], (lines - 1) * pitch + max(0, ll), under

    def _snapshot(self, keep: bool) -> LcdFrame | None:
        g = self.geometry()
        regs = [self._layer_regs(0), self._layer_regs(1), {"BCCR": self.get("BCCR"), "GCR": self.get("GCR")}]
        data, bad = [None, None], ["", ""]
        flags = 0
        for li in (0, 1):
            plan = self._fetch_plan(li, regs[li], g)
            if plan is None:
                continue
            addr, n, under = plan
            if under:
                flags |= FUIF
                bad[li] = "underrun"
                self._warn_once(f"fu{li}", f"{self.name}: layer {li + 1} frame buffer too small for its window "
                                f"(L{li + 1}CFBLR line length / CFBLNR lines): FIFO underrun (RM0090 16.4.2)")
            try:
                for lo, hi in self._no_access:
                    if addr < hi and addr + n > lo:
                        raise BusError(addr)
                if keep:
                    data[li] = self.chip.bus.read_bytes(addr, n)
                else:
                    self.chip.bus.read_bytes(addr, 1)
                    self.chip.bus.read_bytes(addr + n - 1, 1)
            except BusError:
                flags |= TERRIF
                bad[li] = "terr"
                data[li] = None
                self._warn_once(f"te{li}", f"{self.name}: layer {li + 1} frame buffer 0x{addr:08X} (+{n} bytes) is "
                                "not readable by the LTDC AHB master: transfer error (RM0090 16.5)")
        if flags:
            self._flag(flags)
        if not keep:
            return None
        for li in (0, 1):
            if data[li] is None and regs[li]["CR"] & LEN and not bad[li]:
                bad[li] = "terr"
        fr = LcdFrame(self.frames, self.chip.now_ns(), g, regs, [list(c) for c in self.cluts], data, bad)
        self.last_frame = fr
        return fr

    # ------------------------------------------------------------------ board hook / API
    def compose(self) -> LcdFrame:
        """The frame the current registers and memory would display (does not touch the flags)."""
        g = self.geometry()
        regs = [self._layer_regs(0), self._layer_regs(1), {"BCCR": self.get("BCCR"), "GCR": self.get("GCR")}]
        data, bad = [None, None], ["", ""]
        for li in (0, 1):
            plan = self._fetch_plan(li, regs[li], g)
            if plan is None:
                continue
            try:
                data[li] = self.chip.bus.read_bytes(plan[0], plan[1])
            except BusError:
                bad[li] = "terr"
        return LcdFrame(self.frames, self.chip.now_ns(), g, regs, [list(c) for c in self.cluts], data, bad)

    def lcd_attach(self, rx: Any) -> None:
        if rx not in self.receivers:
            self.receivers.append(rx)

    def lcd_detach(self, rx: Any) -> None:
        if rx in self.receivers:
            self.receivers.remove(rx)

    def lcd_accepts(self, pin: Any) -> bool:
        """Is `pin` (on the receiver's clock net) muxed to LCD_CLK?"""
        if self.chip is None or getattr(self, "absent", False):
            return False
        return self.chip.pins.signal_matches(pin.owner, "LCD_CLK")

    def lcd_wiring(self, ports: dict) -> dict:
        """`ports` = {port name: set of this chip's pin ids on that port's net} for the receiver ports clk, hsync,
        vsync, de, r0..r7, g0..g7, b0..b7.  Returns {"clk"/"hsync"/"vsync"/"de": carried?, "mask": (r, g, b) bit
        masks of the data lines carried by pins muxed to LCD_R/G/B, "data_wired": any data port connected,
        "polarity": {"hsync"/"vsync"/"de": active level, "pclk": "rising"/"falling"}}."""
        board = self.chip.pins

        def carried(port: str, sig: str) -> bool:
            for pid in ports.get(port, ()):
                try:
                    p = board.get(pid)
                except (KeyError, ValueError):
                    continue
                if board.signal_matches(p.owner, sig):
                    return True
            return False
        mask = []
        wired = False
        for c in "rgb":
            m = 0
            for k in range(8):
                if ports.get(f"{c}{k}"):
                    wired = True
                if carried(f"{c}{k}", f"LCD_{c.upper()}{k}"):
                    m |= 1 << k
            mask.append(m)
        gcr = self.get("GCR")
        return {"clk": carried("clk", "LCD_CLK"), "hsync": carried("hsync", "LCD_HSYNC"),
                "vsync": carried("vsync", "LCD_VSYNC"), "de": carried("de", "LCD_DE"), "mask": tuple(mask),
                "data_wired": wired,
                "polarity": {"hsync": "H" if gcr & HSPOL else "L", "vsync": "H" if gcr & VSPOL else "L",
                             "de": "H" if gcr & DEPOL else "L", "pclk": "falling" if gcr & PCPOL else "rising"}}

    def status(self) -> dict:
        g = self.geometry()
        x, y = self.position()
        return {"running": self._running, "frames": self.frames, "pixel_clock_hz": self.pixel_clock_hz(),
                "frame_hz": round(self.frame_hz(), 3), "active": f"{g['aaw'] - g['ahbp']}x{g['aah'] - g['avbp']}",
                "x": x, "y": y, "layers": [PF_NAMES[self.get(f"L{k}PFCR") & 7] if self.get(f"L{k}CR") & LEN else "off"
                                           for k in (1, 2)]}
