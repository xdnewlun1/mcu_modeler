"""STM32F42xxx/43xxx Chrom-ART Accelerator (DMA2D, RM0090 Rev 22 chapter 11).

Registers (SVD STM32F427.svd names DMA2D_CR ... DMA2D_AMTCR) at offsets 0x00-0x4C; the foreground CLUT memory at
offset 0x400 and the background CLUT memory at 0x800 (256 x 32-bit words each, 11.3.5), so the block is 0xC00 bytes.
Bits that RM0090 11.5 marks reserved are read-only 0 (the SVD also lists F7-only fields CSS/AI/RBS, which RM0090
calls reserved).

Modes (CR.MODE, 11.3.11 / 11.5.1): 00 memory-to-memory (raw copy, FG CM gives the bits per pixel of input AND output),
01 memory-to-memory with FG PFC, 10 memory-to-memory with FG/BG PFC and blending, 11 register-to-memory (fill with
OCOLR, which holds the colour already packed in the output format: 32/24/16 low bits).

Pixel formats (Tables 53/54/58/59): input ARGB8888, RGB888, RGB565, ARGB1555, ARGB4444, L8, AL44, AL88, L4, A8, A4
(4-bit formats: pixel 0 in the low nibble); output ARGB8888, RGB888, RGB565, ARGB1555, ARGB4444.
* Input expansion to 8 bits per channel copies the MSBs into the LSBs (11.3.4): 5-bit x -> (x << 3) | (x >> 2),
  6-bit -> (x << 2) | (x >> 4), 4-bit -> x * 0x11, 1-bit alpha -> 0x00 / 0xFF; formats without alpha get 0xFF.
* L8/L4: colour and alpha from the CLUT entry; AL44/AL88: RGB from the CLUT entry, alpha from the pixel; A8/A4:
  RGB from FGCOLR/BGCOLR, alpha from the pixel.
* Alpha mode AM (Table 55): 00 keep, 01 replace by ALPHA, 10 alpha x ALPHA / 255 (floor); 11 is reserved and is
  treated as 00 (one `warn`).
* Output conversion keeps the MSBs (truncation): RGB565 = R>>3, G>>2, B>>3; ARGB1555 alpha bit = A>>7; ARGB4444
  = x>>4 (RM0090 does not specify the rounding; truncation is our choice).
* Blender (11.3.6), integer arithmetic, every division rounded down as the RM says:
  aMult = aFG * aBG / 255; aOUT = aFG + aBG - aMult; C = (CFG*aFG + CBG*aBG - CBG*aMult) / aOUT (C = 0 when aOUT = 0,
  a case the RM leaves open). The output alpha is aOUT.

CLUT (11.3.5): automatic loading by FGPFCCR.START / BGPFCCR.START: CS + 1 entries from FGCMAR/BGCMAR, CCM 0 =
ARGB8888 words, CCM 1 = packed RGB888 (B, G, R bytes); stored in the CLUT memory as one 32-bit word per entry
(RGB888 entries as 0xFFRRGGBB; a lookup with CCM = 1 always uses alpha 0xFF). Completion sets CTCIF and clears START.
Manual loading: CPU writes to 0x400/0x800 (word per entry, ARGB8888 layout). A CPU access to a CLUT while it is being
loaded, or while a transfer that reads it (indirect FG format in modes 01/10, indirect BG format in mode 10) runs, sets
CAEIF; the read returns 0 and the write is ignored (11.3.5, 11.3.14).

Transfer control (11.3.12, 11.5.1): CR.START launches a transfer after the configuration check of 11.3.11 (wrong
configuration: CEIF, START cleared, nothing transferred): FGMAR/BGMAR/OMAR alignment with the pixel size (32-bit
formats 4-byte, 16-bit formats 2-byte, others byte aligned), invalid CM codes, PL or FGOR/BGOR odd with a 4-bit
format, NL = 0, PL = 0 (each check only in the modes the RM lists); the CLUT loads check CMAR alignment with CCM.
START while another operation (transfer or CLUT load) runs is cleared without starting (the PFCCR.START case) or
ignored (CR.START stays set by the running transfer). CR.MODE cannot change during a transfer. The configuration
registers (FG/BG MAR, OR, PFCCR, COLR, CMAR, OPFCCR, OCOLR, OMAR, OOR, NLR, LWR) are read-only while an operation
runs (writes ignored, one `warn`). SUSP pauses at the next line boundary; clearing it resumes. ABORT stops the
operation: START, SUSP and ABORT clear, TCIF/CTCIF are not set (lines already written stay written).

Timing: the transfer runs line by line on the core (HCLK) clock through the scheduler: each line takes
max(pixel cycles, AHB beats x (1 + dead time)) HCLK cycles, where AHB beats = 32-bit words read from FG (+ BG in
blending mode) plus words written to the output (byte counts rounded up to whole words), the pixel cycles are PL in
PFC/blending modes (one pixel per clock in the pipeline) and 0 in the copy/fill modes, and the dead time is
AMTCR.DT when AMTCR.EN = 1 (11.3.15). The line's memory effect happens at the end of its time slot, so the output
fills progressively; TWIF is set when line LW (0-based line number, LW < NL) has been written (11.3.13); TCIF at the
end. A CLUT load takes (32-bit words read) x (1 + dead time) cycles and writes the CLUT memory at its end. This is an
approximation of the AHB matrix: wait states, bus contention with the CPU/DMA and FIFO bursts are not modelled.

Errors: TEIF when a read or write hits an address the bus cannot resolve (outside the auto-stub windows) or the CCM
data RAM (a memory region named "CCM*": the DMA2D master port cannot reach it, RM0090 2.1/Figure 1); the operation
stops, START clears (11.3.14).  IRQ "DMA2D" (90) is the level of ISR & CR[13:8] (Table 60). IFCR (write 1 to clear)
reads 0. Clock gate RCC_AHB1ENR.DMA2DEN (one `warn` when accessed while gated; transfers still run, like the rest of
the family); RCC_AHB1RSTR.DMA2DRST / chip reset stop the operation and clear the CLUT memories.
"""
from __future__ import annotations

import struct
from typing import Any

from ...bus import BusError
from ..base import pure_read
from ..registry import register
from ._util import GatedBlock, build_regs, irq_number

# CR
START, SUSP, ABORT = 1 << 0, 1 << 1, 1 << 2
TEIE, TCIE, TWIE, CAEIE, CTCIE, CEIE = (1 << b for b in range(8, 14))
MODE_SHIFT = 16
M2M, M2M_PFC, M2M_BLEND, R2M = 0, 1, 2, 3
MODE_NAMES = ("memory-to-memory", "memory-to-memory with PFC", "memory-to-memory with blending",
              "register-to-memory")
# ISR / IFCR
TEIF, TCIF, TWIF, CAEIF, CTCIF, CEIF = (1 << b for b in range(6))
# PFCCR
PFC_START = 1 << 5
CCM_RGB888 = 1 << 4

IN_FORMATS = ("ARGB8888", "RGB888", "RGB565", "ARGB1555", "ARGB4444", "L8", "AL44", "AL88", "L4", "A8", "A4")
IN_BITS = (32, 24, 16, 16, 16, 8, 8, 16, 4, 8, 4)
OUT_FORMATS = IN_FORMATS[:5]
INDIRECT = {5, 6, 7, 8}                  # L8, AL44, AL88, L4 use the CLUT
FG_CLUT, BG_CLUT, CLUT_BYTES = 0x400, 0x800, 0x400

# writable bits per RM0090 11.5 (everything else reserved, reads 0)
_VALID = {"DMA2D_CR": 0x00033F07, "DMA2D_FGOR": 0x3FFF, "DMA2D_BGOR": 0x3FFF, "DMA2D_OOR": 0x3FFF,
          "DMA2D_FGPFCCR": 0xFF03FF3F, "DMA2D_BGPFCCR": 0xFF03FF3F, "DMA2D_FGCOLR": 0xFFFFFF,
          "DMA2D_BGCOLR": 0xFFFFFF, "DMA2D_OPFCCR": 0x7, "DMA2D_NLR": 0x3FFFFFFF, "DMA2D_LWR": 0xFFFF,
          "DMA2D_AMTCR": 0xFF01, "DMA2D_IFCR": 0x3F}
_PROTECTED = ("DMA2D_FGMAR", "DMA2D_FGOR", "DMA2D_BGMAR", "DMA2D_BGOR", "DMA2D_FGCOLR", "DMA2D_BGCOLR",
              "DMA2D_FGCMAR", "DMA2D_BGCMAR", "DMA2D_OPFCCR", "DMA2D_OCOLR", "DMA2D_OMAR", "DMA2D_OOR",
              "DMA2D_NLR", "DMA2D_LWR")

# --------------------------------------------------------------------------------- pixel conversion helpers
_T565: list[int] | None = None
_T1555: list[int] | None = None
_T4444: list[int] | None = None


def _e5(x: int) -> int:
    return (x << 3) | (x >> 2)


def _tables() -> None:
    global _T565, _T1555, _T4444
    if _T565 is not None:
        return
    _T565 = [0xFF000000 | _e5(v >> 11) << 16 | (((v >> 5) & 63) << 2 | ((v >> 5) & 63) >> 4) << 8 | _e5(v & 31)
             for v in range(65536)]
    _T1555 = [(0xFF000000 if v & 0x8000 else 0) | _e5((v >> 10) & 31) << 16 | _e5((v >> 5) & 31) << 8 | _e5(v & 31)
              for v in range(65536)]
    _T4444 = [((v >> 12) * 17) << 24 | ((v >> 8 & 15) * 17) << 16 | ((v >> 4 & 15) * 17) << 8 | (v & 15) * 17
              for v in range(65536)]


def decode(cm: int, data: bytes, n: int, clut: list[int] | None = None, color: int = 0) -> list[int]:
    """n pixels of input format `cm` (Table 53) -> list of 0xAARRGGBB (11.3.4 expansion). `clut`: 256 ARGB entries
    (indirect formats), `color`: 0xRRGGBB (A8/A4)."""
    if cm == 0:
        return list(struct.unpack_from(f"<{n}I", data))
    if cm == 1:
        d = data
        return [0xFF000000 | d[3 * k + 2] << 16 | d[3 * k + 1] << 8 | d[3 * k] for k in range(n)]
    if cm in (2, 3, 4):
        _tables()
        t = (_T565, _T1555, _T4444)[cm - 2]
        return [t[v] for v in struct.unpack_from(f"<{n}H", data)]
    color &= 0xFFFFFF
    if cm == 5:
        return [clut[b] for b in data[:n]]
    if cm == 6:
        return [((b >> 4) * 17) << 24 | (clut[b & 15] & 0xFFFFFF) for b in data[:n]]
    if cm == 7:
        return [data[2 * k + 1] << 24 | (clut[data[2 * k]] & 0xFFFFFF) for k in range(n)]
    if cm == 8:
        return [clut[(data[k >> 1] >> (4 * (k & 1))) & 15] for k in range(n)]
    if cm == 9:
        return [b << 24 | color for b in data[:n]]
    if cm == 10:
        return [(((data[k >> 1] >> (4 * (k & 1))) & 15) * 17) << 24 | color for k in range(n)]
    raise ValueError(f"invalid input colour mode {cm}")


def alpha_mod(px: list[int], am: int, alpha: int) -> list[int]:
    """Table 55: 00 keep, 01 replace by ALPHA, 10 original x ALPHA / 255 (rounded down)."""
    if am == 1:
        a = alpha << 24
        return [p & 0xFFFFFF | a for p in px]
    if am == 2:
        return [((p >> 24) * alpha // 255) << 24 | (p & 0xFFFFFF) for p in px]
    return px


def blend_pixel(fg: int, bg: int) -> int:
    """RM0090 11.3.6 blender (divisions rounded to the nearest lower integer)."""
    fa, ba = fg >> 24, bg >> 24
    am = fa * ba // 255
    ao = fa + ba - am
    if ao == 0:
        return 0
    out = ao << 24
    for sh in (16, 8, 0):
        cf, cb = (fg >> sh) & 255, (bg >> sh) & 255
        out |= ((cf * fa + cb * ba - cb * am) // ao) << sh
    return out


def encode(cm: int, px: list[int]) -> bytes:
    """ARGB8888 values -> output format `cm` (Table 58/59), keeping the MSBs."""
    if cm == 0:
        return struct.pack(f"<{len(px)}I", *px)
    if cm == 1:
        out = bytearray(3 * len(px))
        for k, p in enumerate(px):
            out[3 * k] = p & 255
            out[3 * k + 1] = (p >> 8) & 255
            out[3 * k + 2] = (p >> 16) & 255
        return bytes(out)
    if cm == 2:
        v = [((p >> 19) & 31) << 11 | ((p >> 10) & 63) << 5 | ((p >> 3) & 31) for p in px]
    elif cm == 3:
        v = [(p >> 31) << 15 | ((p >> 19) & 31) << 10 | ((p >> 11) & 31) << 5 | ((p >> 3) & 31) for p in px]
    elif cm == 4:
        v = [(p >> 28) << 12 | ((p >> 20) & 15) << 8 | ((p >> 12) & 15) << 4 | ((p >> 4) & 15) for p in px]
    else:
        raise ValueError(f"invalid output colour mode {cm}")
    return struct.pack(f"<{len(v)}H", *v)


def _align(bits: int) -> int:
    return 4 if bits == 32 else 2 if bits == 16 else 1


def _nbytes(bits: int, pixels: int) -> int:
    return (bits * pixels + 7) // 8


class _Fault(Exception):
    def __init__(self, addr: int, msg: str):
        super().__init__(msg)
        self.addr = addr
        self.msg = msg


@register("stm32.dma2d")
class DMA2D(GatedBlock):
    model = "stm32.dma2d"

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.gate = params.get("gate", "DMA2D")
        ov: dict[str, dict] = {n: {"ro_mask": 0xFFFFFFFF & ~v} for n, v in _VALID.items()}
        ov["DMA2D_CR"]["on_write"] = self._cr_write
        ov["DMA2D_ISR"] = {"ro_mask": 0xFFFFFFFF}
        ov["DMA2D_IFCR"].update(on_write=self._ifcr_write, on_read=pure_read(lambda b, r: 0))
        ov["DMA2D_FGPFCCR"]["on_write"] = self._pfccr_write("fg")
        ov["DMA2D_BGPFCCR"]["on_write"] = self._pfccr_write("bg")
        for n in _PROTECTED:
            ov.setdefault(n, {})["on_write"] = self._prot_write
        r = build_regs((name, "DMA2D"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self._irq: int | None = None
        self._line_lvl = False
        self._ev = None
        self.busy: str | None = None          # None | "xfer" | "fgclut" | "bgclut"
        self._paused = False
        self._line = 0
        self._job: dict = {}
        self._no_access: list[tuple[int, int]] = []
        self._warned: set[str] = set()
        self.transfers = 0                     # completed transfers (statistics)
        self.pixels = 0

    # ------------------------------------------------------------------ setup
    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irq = irq_number(chip, self.name, "DMA2D")
        self._no_access = []
        for m in getattr(getattr(chip, "desc", None), "memory", None) or []:
            if str(getattr(m, "name", "")).upper().startswith("CCM"):
                self._no_access.append((m.base, m.base + m.size))

    def reset(self) -> None:
        self._cancel()
        self.busy = None
        self._paused = False
        self._job = {}
        self._warned.clear()
        super().reset()
        self._update_irq()

    def _cancel(self) -> None:
        if self._ev is not None and self.chip is not None:
            self.chip.sched.cancel(self._ev)
        self._ev = None

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=msg)

    # ------------------------------------------------------------------ AHB slave accesses
    def _clut_conflict(self, offset: int) -> bool:
        if offset < FG_CLUT or offset >= BG_CLUT + CLUT_BYTES or self.busy is None:
            return False
        fg = offset < BG_CLUT
        if self.busy == "fgclut":
            return fg
        if self.busy == "bgclut":
            return not fg
        j = self._job
        if fg:
            return j.get("mode") in (M2M_PFC, M2M_BLEND) and j.get("fgcm") in INDIRECT
        return j.get("mode") == M2M_BLEND and j.get("bgcm") in INDIRECT

    def read(self, offset: int, size: int) -> int:
        if self._gate_loc is not None:
            self._gate_ok()
        if offset >= FG_CLUT and self._clut_conflict(offset):
            self._caeif("read")
            return 0
        return super().read(offset, size)

    def write(self, offset: int, size: int, value: int) -> None:
        if self._gate_loc is not None:
            self._gate_ok()
        if offset >= FG_CLUT and self._clut_conflict(offset):
            self._caeif("write")
            return
        super().write(offset, size, value)

    def _caeif(self, what: str) -> None:
        self._warn_once("caeif", f"{self.name}: CPU CLUT {what} while the CLUT is in use by the DMA2D: CAEIF set, "
                        "access ignored (RM0090 11.3.14)")
        self._flag(CAEIF)

    # ------------------------------------------------------------------ flags / IRQ
    def _flag(self, bits: int) -> None:
        self.set("DMA2D_ISR", self.get("DMA2D_ISR") | bits)
        self._update_irq()

    def _update_irq(self) -> None:
        lvl = bool(self.get("DMA2D_ISR") & (self.get("DMA2D_CR") >> 8) & 0x3F)
        if lvl != self._line_lvl and self._irq is not None and self.chip is not None:
            self.irq(self._irq, lvl)
        self._line_lvl = lvl

    def _ifcr_write(self, blk, reg, old, new):
        if new & 0x3F:
            self.set("DMA2D_ISR", self.get("DMA2D_ISR") & ~new & 0x3F)
            self._update_irq()
        return 0

    # ------------------------------------------------------------------ register hooks
    def _prot_write(self, blk, reg, old, new):
        if self.busy is not None and new != old:
            self._warn_once("prot", f"{self.name}: {reg.name} written while a DMA2D operation runs: ignored (the "
                            "register is read-only once the transfer has started, RM0090 11.5)")
            return old
        return None

    def _pfccr_write(self, which: str):
        def wr(blk, reg, old, new):
            if self.busy is not None:
                if new != old:
                    keep = old & ~PFC_START
                    if (new ^ old) & ~PFC_START:
                        self._warn_once("prot", f"{self.name}: {reg.name} written while a DMA2D operation runs: "
                                        "ignored (RM0090 11.5.8/11.5.10)")
                    if new & PFC_START and not old & PFC_START:
                        return keep          # another operation is ongoing: START reset, nothing started
                    return old
                return None
            if new & PFC_START:
                self._store(reg, new)
                self._start_clut(which)
                return self.get(reg.name)
            return None
        return wr

    def _cr_write(self, blk, reg, old, new):
        cr = new
        if self.busy is not None:
            cr = (cr & ~(3 << MODE_SHIFT)) | (old & (3 << MODE_SHIFT))     # MODE cannot change during a transfer
            if self.busy == "xfer":
                cr |= START
            else:
                cr = (cr & ~START) | (old & START)                           # CLUT loading: no data transfer start
            if cr & ABORT:
                self._store(reg, cr)
                self._abort()
                self._update_irq()
                return self.get("DMA2D_CR")
            if self._paused and not cr & SUSP:
                self._paused = False
                self._store(reg, cr)
                self._schedule_next()
        else:
            cr &= ~ABORT                                                     # reset by hardware with START = 0
            if cr & START and not old & START:
                self._store(reg, cr)
                self._start_xfer()
                cr = self.get("DMA2D_CR")
        self._store(reg, cr)
        self._update_irq()
        return cr

    # ------------------------------------------------------------------ configuration
    def _pfc(self, which: str) -> tuple[int, int, int, int, int, int]:
        v = self.get("DMA2D_FGPFCCR" if which == "fg" else "DMA2D_BGPFCCR")
        return v & 15, (v >> 4) & 1, (v >> 8) & 255, (v >> 16) & 3, v >> 24, v   # cm, ccm, cs, am, alpha, raw

    def _config_error(self, mode: int) -> str | None:
        nlr = self.get("DMA2D_NLR")
        pl, nl = (nlr >> 16) & 0x3FFF, nlr & 0xFFFF
        fgcm = self.get("DMA2D_FGPFCCR") & 15
        bgcm = self.get("DMA2D_BGPFCCR") & 15
        ocm = self.get("DMA2D_OPFCCR") & 7
        if mode != R2M:
            if fgcm > 10:
                return f"FGPFCCR.CM = {fgcm} is invalid"
            if self.get("DMA2D_FGMAR") % _align(IN_BITS[fgcm]):
                return f"FGMAR 0x{self.get('DMA2D_FGMAR'):08x} not aligned for {IN_FORMATS[fgcm]}"
            if IN_BITS[fgcm] == 4 and (pl & 1 or self.get("DMA2D_FGOR") & 1):
                return "PL or FGOR.LO odd with a 4-bit foreground format"
        if mode == M2M_BLEND:
            if bgcm > 10:
                return f"BGPFCCR.CM = {bgcm} is invalid"
            if self.get("DMA2D_BGMAR") % _align(IN_BITS[bgcm]):
                return f"BGMAR 0x{self.get('DMA2D_BGMAR'):08x} not aligned for {IN_FORMATS[bgcm]}"
            if IN_BITS[bgcm] == 4 and (pl & 1 or self.get("DMA2D_BGOR") & 1):
                return "PL or BGOR.LO odd with a 4-bit background format"
        if mode != M2M:
            if ocm > 4:
                return f"OPFCCR.CM = {ocm} is invalid"
            if self.get("DMA2D_OMAR") % _align(IN_BITS[ocm]):
                return f"OMAR 0x{self.get('DMA2D_OMAR'):08x} not aligned for {OUT_FORMATS[ocm]}"
        if nl == 0:
            return "NLR.NL = 0"
        if pl == 0:
            return "NLR.PL = 0"
        return None

    def _config_fail(self, why: str) -> None:
        self.emit("warn", block=self.name, msg=f"{self.name}: configuration error ({why}): CEIF set, transfer not "
                  "started (RM0090 11.3.11)")
        self._flag(CEIF)

    def _dead(self) -> int:
        amtcr = self.get("DMA2D_AMTCR")
        return (amtcr >> 8) & 255 if amtcr & 1 else 0

    # ------------------------------------------------------------------ data transfer
    def _start_xfer(self) -> None:
        cr = self.get("DMA2D_CR")
        mode = (cr >> MODE_SHIFT) & 3
        why = self._config_error(mode)
        if why is not None:
            self.set("DMA2D_CR", cr & ~(START | SUSP | ABORT))
            self._config_fail(why)
            return
        nlr = self.get("DMA2D_NLR")
        fgcm, _, _, fgam, fga, _ = self._pfc("fg")
        bgcm, _, _, bgam, bga, _ = self._pfc("bg")
        ocm = self.get("DMA2D_OPFCCR") & 7
        for which, am in (("fg", fgam), ("bg", bgam)):
            if am == 3 and mode in (M2M_PFC, M2M_BLEND) and (which == "fg" or mode == M2M_BLEND):
                self._warn_once(f"am3{which}", f"{self.name}: {which.upper()}PFCCR.AM = 11 is reserved (RM0090 "
                                "Table 55): treated as no modification")
        pl, nl = (nlr >> 16) & 0x3FFF, nlr & 0xFFFF
        j = {"mode": mode, "pl": pl, "nl": nl, "fgcm": fgcm, "bgcm": bgcm, "ocm": ocm,
             "fgam": fgam, "fga": fga, "bgam": bgam, "bga": bga,
             "fgmar": self.get("DMA2D_FGMAR"), "bgmar": self.get("DMA2D_BGMAR"), "omar": self.get("DMA2D_OMAR"),
             "fgor": self.get("DMA2D_FGOR"), "bgor": self.get("DMA2D_BGOR"), "oor": self.get("DMA2D_OOR"),
             "lw": self.get("DMA2D_LWR") & 0xFFFF}
        fg_bits = IN_BITS[fgcm] if fgcm <= 10 else 32
        if mode == R2M:
            j["in_bytes"] = 0
            j["out_bits"] = IN_BITS[ocm]
        elif mode == M2M:
            j["in_bytes"] = _nbytes(fg_bits, pl)
            j["out_bits"] = fg_bits
        else:
            j["in_bytes"] = _nbytes(fg_bits, pl)
            if mode == M2M_BLEND:
                j["bg_bytes"] = _nbytes(IN_BITS[bgcm], pl)
            j["out_bits"] = IN_BITS[ocm]
        beats = (-(-j["in_bytes"] // 4) + -(-j.get("bg_bytes", 0) // 4)
                 + -(-_nbytes(j["out_bits"], pl) // 4))
        pix = pl if mode in (M2M_PFC, M2M_BLEND) else 0
        j["line_cycles"] = max(pix, beats * (1 + self._dead()), 1)
        self._job = j
        self.busy = "xfer"
        self._line = 0
        self._paused = False
        self._schedule_next()

    def _schedule_next(self) -> None:
        self._cancel()
        if self.busy == "xfer":
            self._ev = self.chip.sched.in_cycles(self._job["line_cycles"], self._line_done)
        else:
            self._ev = self.chip.sched.in_cycles(self._job["cycles"], self._clut_done)

    def _check(self, addr: int, n: int) -> None:
        for lo, hi in self._no_access:
            if addr < hi and addr + n > lo:
                raise _Fault(addr, "CCM data RAM is not reachable by the DMA2D master port")

    def _rd(self, addr: int, n: int) -> bytes:
        self._check(addr, n)
        try:
            return self.chip.bus.read_bytes(addr, n)
        except BusError as e:
            raise _Fault(getattr(e, "addr", addr), str(e)) from None

    def _wr(self, addr: int, data: bytes) -> None:
        self._check(addr, len(data))
        try:
            self.chip.bus.write_bytes(addr, data)
        except BusError as e:
            raise _Fault(getattr(e, "addr", addr), str(e)) from None

    def _clut(self, which: str) -> list[int]:
        base = FG_CLUT if which == "fg" else BG_CLUT
        words = struct.unpack_from("<256I", self.mem, base)
        if self._pfc(which)[1]:                       # CCM = RGB888: opaque entries
            return [0xFF000000 | (w & 0xFFFFFF) for w in words]
        return list(words)

    def _line_done(self) -> None:
        self._ev = None
        if self.busy != "xfer":
            return
        if self.get("DMA2D_CR") & SUSP:
            self._paused = True
            return
        j = self._job
        i = self._line
        try:
            self._do_line(j, i)
        except _Fault as e:
            self.emit("warn", block=self.name, msg=f"{self.name}: bus error at 0x{e.addr:08x} ({e.msg}) on line "
                      f"{i}: TEIF set, transfer stopped (RM0090 11.3.14)")
            self._finish(TEIF)
            return
        self.pixels += j["pl"]
        self._line = i + 1
        flags = TWIF if i == j["lw"] else 0
        if self._line >= j["nl"]:
            self.transfers += 1
            self._finish(flags | TCIF)
            return
        if flags:
            self._flag(flags)
        self._schedule_next()

    def _do_line(self, j: dict, i: int) -> None:
        mode, pl = j["mode"], j["pl"]
        obits = j["out_bits"]
        oaddr = j["omar"] + (i * (pl + j["oor"]) * obits) // 8
        if mode == R2M:
            nb = obits // 8
            self._wr(oaddr, self.get("DMA2D_OCOLR").to_bytes(4, "little")[:nb] * pl)
            return
        fbits = IN_BITS[j["fgcm"]]
        faddr = j["fgmar"] + (i * (pl + j["fgor"]) * fbits) // 8
        fdata = self._rd(faddr, j["in_bytes"])
        if mode == M2M:
            self._wr(oaddr, fdata)
            return
        fg = decode(j["fgcm"], fdata, pl, self._clut("fg") if j["fgcm"] in INDIRECT else None,
                    self.get("DMA2D_FGCOLR"))
        fg = alpha_mod(fg, j["fgam"], j["fga"])
        if mode == M2M_BLEND:
            bbits = IN_BITS[j["bgcm"]]
            baddr = j["bgmar"] + (i * (pl + j["bgor"]) * bbits) // 8
            bdata = self._rd(baddr, j["bg_bytes"])
            bg = decode(j["bgcm"], bdata, pl, self._clut("bg") if j["bgcm"] in INDIRECT else None,
                        self.get("DMA2D_BGCOLR"))
            bg = alpha_mod(bg, j["bgam"], j["bga"])
            fg = [blend_pixel(f, b) for f, b in zip(fg, bg)]
        self._wr(oaddr, encode(j["ocm"], fg))

    def _finish(self, flags: int) -> None:
        self._cancel()
        busy = self.busy
        self.busy = None
        self._paused = False
        self.set("DMA2D_CR", self.get("DMA2D_CR") & ~(START | SUSP | ABORT))
        if busy in ("fgclut", "bgclut"):
            reg = "DMA2D_FGPFCCR" if busy == "fgclut" else "DMA2D_BGPFCCR"
            self.set(reg, self.get(reg) & ~PFC_START)
        self._flag(flags)

    def _abort(self) -> None:
        self._finish(0)

    # ------------------------------------------------------------------ CLUT automatic loading
    def _start_clut(self, which: str) -> None:
        cm, ccm, cs, _, _, raw = self._pfc(which)
        reg = "DMA2D_FGPFCCR" if which == "fg" else "DMA2D_BGPFCCR"
        cmar = self.get("DMA2D_FGCMAR" if which == "fg" else "DMA2D_BGCMAR")
        if not ccm and cmar & 3:
            self.set(reg, raw & ~PFC_START)
            self._config_fail(f"{which.upper()}CMAR 0x{cmar:08x} not 32-bit aligned for an ARGB8888 CLUT")
            return
        n = cs + 1
        nbytes = 3 * n if ccm else 4 * n
        self._job = {"which": which, "cmar": cmar, "n": n, "ccm": ccm,
                     "cycles": max(-(-nbytes // 4) * (1 + self._dead()), 1)}
        self.busy = f"{which}clut"
        self._paused = False
        self._schedule_next()

    def _clut_done(self) -> None:
        self._ev = None
        if self.busy not in ("fgclut", "bgclut"):
            return
        if self.get("DMA2D_CR") & SUSP:
            self._paused = True
            return
        j = self._job
        try:
            data = self._rd(j["cmar"], 3 * j["n"] if j["ccm"] else 4 * j["n"])
        except _Fault as e:
            self.emit("warn", block=self.name, msg=f"{self.name}: bus error at 0x{e.addr:08x} ({e.msg}) during the "
                      "CLUT load: TEIF set (RM0090 11.3.14)")
            self._finish(TEIF)
            return
        if j["ccm"]:
            words = [0xFF000000 | data[3 * k + 2] << 16 | data[3 * k + 1] << 8 | data[3 * k] for k in range(j["n"])]
        else:
            words = list(struct.unpack(f"<{j['n']}I", data))
        base = FG_CLUT if j["which"] == "fg" else BG_CLUT
        struct.pack_into(f"<{j['n']}I", self.mem, base, *words)
        self._finish(CTCIF)
