"""STM32F4 DCMI (digital camera interface, RM0090 ch. 15), STM32F407/F417 only.

Registers from the SVD (CR, SR, RIS, IER, MIS, ICR, ESCR, ESUR, CWSTRT, CWSIZE, DR); behaviour hand-written from
RM0090 Rev 22 ch. 15 (section numbers below), checked against the STM32CubeF4 HAL (stm32f4xx_hal_dcmi.c).

* Presence: `params.only` (chip YAML: STM32F407, STM32F417).  On an F405/F415 the block behaves like the `stub`
  model (`_util.make_absent`).  Clock gate RCC_AHB2ENR.DCMIEN (one `warn` when accessed gated off); RCC_AHB2RSTR
  resets the block.
* Input: a parallel camera on the board (chipem/parts/camera.py) delivers one line of pixel-clock samples at a time
  through the vendor-free DVP hook (`dvp_accepts(pin)`, `dvp_line(src, t0_ns, period_ns, samples, ports)`, see the
  camera docstring); the model turns it into 32-bit words at the instant the last sample of each word is clocked in
  (one scheduler event per word: t0 + (k + 1/2) x period for sample k).  HSYNC / VSYNC are real pin levels: the
  model listens to the pins muxed to DCMI_HSYNC / DCMI_VSYNC (board nets).  PIXCLK is not toggled (the camera logs
  it as a `wave` summary); PCKPOL is stored only.
* Polarity (CR.HSPOL / VSPOL): the level of the "active" sync state, i.e. blanking (data are captured while HSYNC
  and VSYNC are *not* at that level).  SR.HSYNC / SR.VSYNC read 1 during horizontal / vertical blanking
  (while ENABLE = 1; 0 otherwise, the reset value).
* Capture (CR.ENABLE + CR.CAPTURE, 15.5.4): starts at the next frame start (VSYNC leaving blanking); CM = 1
  (snapshot) captures one frame and clears CAPTURE at its end; CM = 0 (continuous) captures every frame selected by
  FCRC (all, 1 of 2, 1 of 4); a CAPTURE cleared by software in continuous mode stops after the current frame.  CROP
  (15.5.5, 15.8.9/10): the window starts at line VST and pixel clock HOFFCNT of each line and is VLINE + 1 lines by
  CAPCNT + 1 pixel clocks (counts in pixel clocks, not pixels); FRAME is set at the end of the last line of the window
  (15.8.3), or at VSYNC if the frame ends first.  JPEG = 1 (15.5.6): no crop, no embedded sync (ESS ignored), HSYNC is
  a data-valid signal, the last word of a frame is padded.
* Packing (EDM): 8-bit = 4 samples per word (first sample in bits 7:0); 10/12/14-bit = 2 samples per word (first in
  bits 15:0), each masked to the data width.  A partial word at frame end is padded with zeros.  Only the data lines
  D0..D(n-1) of the selected width are used.  A data bit whose DCMI_Dk pin is not muxed reads 0; a muxed pin that is
  not on the camera's d<k> net reads its own level (H = 1) for the whole line.
* Embedded synchronisation (ESS = 1, 8-bit only, 15.5.3): the stream carries 0xFF 0x00 0x00 XY codes; XY is
  compared with ESCR.FSC / LSC / LEC / FEC under the ESUR unmask bits.  Frame start replaces the first line start,
  frame end the last line end (the camera part's convention).  After ENABLE is set, capture starts only at the first
  FS that follows an FE (15.5.3 mode 2).  FSC = 0xFF: the first LSC after an FEC starts the frame; FEC = 0xFF: every
  unrecognised code ends the frame (15.8.7).  A line start inside a line, a line end outside one, or an unknown code
  sets ERR_RIS; like an overrun it resets the FIFO and the frame is dropped until the next frame start (15.5.7).  The
  HSYNC/VSYNC pins are ignored; LINE / VSYNC flags are set only while CAPTURE = 1 (15.8.3).
* FIFO: 4 x 32-bit words (RM0090 15.5.7 "four-word FIFO", 15.8.11 "4-word deep FIFO"; SR.FNE).  DR pops the oldest
  word (0 when empty).  A word that finds the FIFO full sets OVR_RIS; the FIFO is reset and the rest of the frame is
  lost, capture resumes at the next frame start, and in snapshot mode CAPTURE is cleared (15.5.4, 15.5.7; no FRAME
  flag for the lost frame).  DMA: the "DCMI" request (DMA2 stream 1 / 7 channel 1) is asserted while the FIFO is not
  empty (`_util.dma_request`).
* Flags (RIS, 15.8.3): FRAME (end of every captured frame / crop window), OVR, ERR, VSYNC (VSYNC entering blanking /
  FE code; with hardware sync also while CAPTURE = 0, 15.5.4 note), LINE (HSYNC entering blanking / LE code); set only
  while ENABLE = 1.  MIS = RIS & IER, ICR write 1 clears, IRQ "DCMI" (level).
Not modelled: pixel clock polarity effects (PCKPOL stored only), the F42x/F446 BSM/OEBS/LSM/OELS fields (they do not
exist on the F407: written bits are kept but have no effect, one `warn`), sync/data timing violations (PIXCLK period
> 2.5 HCLK, 15.4), JPEG headers, the bus error on non-word register accesses (15.8), the CAPCNT word-alignment rule
(15.5.5: CAPCNT is used as written).
"""
from __future__ import annotations

from collections import deque
from typing import Any

from ..base import MMIOBlock, pure_read
from ..registry import register
from ._util import GatedBlock, build_regs, dma_request, irq_number, make_absent, part_present

CAPTURE, CM, CROP, JPEG, ESS, PCKPOL, HSPOL, VSPOL, ENABLE = 1, 2, 4, 8, 0x10, 0x20, 0x40, 0x80, 0x4000
F42X_BITS = 0x3F << 16                    # BSM, OEBS, LSM, OELS (later DCMI revisions)
SR_HSYNC, SR_VSYNC, SR_FNE = 1, 2, 4
FRAME, OVR, ERR, VSYNC, LINE = 1, 2, 4, 8, 0x10
DEPTH = 4                                 # RM0090 15.5.7 / 15.8.11
EDM_BITS = (8, 10, 12, 14)


@register("stm32.dcmi")
class DCMI(GatedBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.gate = "DCMI"
        ov = {"CR": {"on_write": self._cr_write},
              "SR": {"ro_mask": 0xFFFFFFFF, "on_read": self._sr_read},
              "RIS": {"ro_mask": 0xFFFFFFFF},
              "IER": {"on_write": self._ier_write},
              "MIS": {"ro_mask": 0xFFFFFFFF, "on_read": pure_read(lambda b, r: self.get("RIS") & self.get("IER"))},
              "ICR": {"on_read": pure_read(lambda b, r: 0), "on_write": self._icr_write},
              "DR": {"ro_mask": 0xFFFFFFFF, "on_read": self._dr_read,
                     "on_peek": lambda b, r: 0 if self.absent or not self.fifo else self.fifo[0]}}
        r = build_regs((name, "DCMI"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.absent = False
        self._irqn: int | None = None
        self._listen: set[str] = set()
        self._warned: set[str] = set()
        self.frames_captured = 0
        self.words: deque[int] = deque(maxlen=200_000)    # every word that entered the FIFO (observation)
        self._init_state()

    def _init_state(self) -> None:
        self.fifo: deque[int] = deque()
        self._evs: list = []
        self._line = False
        self._dma = False
        self._capturing = False           # the current frame is being captured
        self._stop_after = False
        self._in_frame = False            # between frame start and frame end (sync)
        self._frame_no = 0                # frames started while capture was requested (FCRC)
        self._y = -1                      # line index within the frame
        self._acc: list[int] = []         # samples of the word being packed
        self._hs_blank = True
        self._vs_blank = True
        # embedded sync
        self._e_inframe = False
        self._e_inline = False
        self._e_after_fe = False          # FE seen since ENABLE (mode 2: capture starts at FE followed by FS)

    # ------------------------------------------------------------------ setup
    def attach(self, chip: Any) -> None:
        super().attach(chip)
        if not part_present(chip, self.params):
            make_absent(self, "the camera interface (DCMI)")
            return
        self._irqn = irq_number(chip, "DCMI")
        board = getattr(chip, "pins", None)
        if board is not None:
            for sig in ("DCMI_HSYNC", "DCMI_VSYNC"):
                for p in board.signal_pins(sig):
                    if (sig, p.id) not in self._listen:
                        self._listen.add((sig, p.id))
                        board.input_listeners[p.id].append(lambda pin, _s=sig: self._sync_pin(_s, pin))

    def reset(self) -> None:
        if self.absent:
            MMIOBlock.reset(self)
            return
        self._cancel_words()
        if self.chip is not None:
            if getattr(self, "_line", False) and self._irqn is not None:
                self.irq(self._irqn, False)
            if getattr(self, "_dma", False):
                dma_request(self.chip, "DCMI", False)
        self._init_state()
        self._warned.clear()
        super().reset()

    def _cancel_words(self) -> None:
        if self.chip is not None:
            for ev in getattr(self, "_evs", ()):
                self.chip.sched.cancel(ev)
        self._evs = []

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=msg)

    # ------------------------------------------------------------------ config helpers
    def _cr(self) -> int:
        return self.get("CR")

    def data_bits(self) -> int:
        return EDM_BITS[(self._cr() >> 10) & 3]

    def _enabled(self) -> bool:
        return bool(self._cr() & ENABLE)

    def _muxed_pin(self, sig: str):
        board = self.chip.pins
        for p in board.signal_pins(sig):
            if board.signal_matches(p.owner, sig):
                return p
        return None

    def _blank(self, sig: str, level: str | None, pol_bit: int) -> bool:
        """Is this sync level the active (blanking) state per the polarity bit?  Floating counts as L."""
        hi = level == "H"
        return hi == bool(self._cr() & pol_bit)

    # ------------------------------------------------------------------ flags / IRQ / DMA
    def _flag(self, f: int) -> None:
        if not self._enabled():
            return
        self.set("RIS", self.get("RIS") | f)
        self._update()

    def _ess(self, cr: int | None = None) -> bool:
        """Embedded synchronisation in effect (ESS is disabled in JPEG mode, RM0090 15.8.1)."""
        cr = self._cr() if cr is None else cr
        return bool(cr & ESS) and not cr & JPEG

    def _sync_flag(self, f: int) -> None:
        """LINE / VSYNC: with embedded sync set only while CAPTURE = 1 (RM0090 15.8.3)."""
        cr = self._cr()
        if self._ess(cr) and not cr & CAPTURE:
            return
        self._flag(f)

    def _abort_frame(self) -> None:
        """Overrun or synchronisation error: the FIFO is reset and the DCMI waits for a new frame start (15.5.7)."""
        self._cancel_words()
        self.fifo.clear()
        self._acc = []
        self._capturing = False
        if self._stop_after:                              # continuous mode, CAPTURE cleared: that frame has ended
            self._stop_after = False
            self.set("CR", self._cr() & ~CAPTURE)
        self._update()

    def _update(self) -> None:
        if self.chip is None or self.absent:
            return
        line = bool(self.get("RIS") & self.get("IER"))
        if line != self._line and self._irqn is not None:
            self._line = line
            self.irq(self._irqn, line)
        dma = bool(self.fifo) and self._enabled()
        if dma != self._dma:
            self._dma = dma
            dma_request(self.chip, "DCMI", dma)

    # ------------------------------------------------------------------ hardware sync (pins)
    def _sync_pin(self, sig: str, pin: Any) -> None:
        if self.absent or self.chip is None:
            return
        if not self.chip.pins.signal_matches(pin.owner, sig):
            return
        cr = self._cr()
        lvl = pin.level()
        if sig == "DCMI_VSYNC":
            blank = self._blank(sig, lvl, VSPOL)
            if blank == self._vs_blank:
                return
            self._vs_blank = blank
            if self._ess(cr):
                return
            if blank:
                self._frame_end()
            else:
                self._frame_start()
        else:
            blank = self._blank(sig, lvl, HSPOL)
            if blank == self._hs_blank:
                return
            self._hs_blank = blank
            if self._ess(cr):
                return
            if blank:
                self._flag(LINE)
                if self._capturing and self._crop_last_line():
                    self._complete_capture()
            elif self._in_frame:
                self._y += 1

    def _frame_start(self) -> None:
        self._in_frame = True
        self._y = -1
        self._acc = []
        cr = self._cr()
        self._capturing = False
        if not (cr & ENABLE and cr & CAPTURE):
            return
        fcrc = (cr >> 8) & 3
        n = self._frame_no
        self._frame_no += 1
        if (fcrc == 1 and n % 2) or (fcrc == 2 and n % 4):
            return
        self._capturing = True
        if not self._ess(cr):
            for s in ("DCMI_HSYNC", "DCMI_VSYNC"):
                if self._muxed_pin(s) is None:
                    self._warn_once("nosync", f"{self.name}: {s} is not muxed to a pin (hardware synchronisation "
                                    f"needs HSYNC and VSYNC)")

    def _crop_window(self) -> tuple[int, int, int, int] | None:
        """(HOFFCNT, VST, CAPCNT + 1, VLINE + 1) when cropping is in effect."""
        cr = self._cr()
        if not cr & CROP or cr & JPEG:
            return None
        st, sz = self.get("CWSTRT"), self.get("CWSIZE")
        return st & 0x3FFF, (st >> 16) & 0x1FFF, (sz & 0x3FFF) + 1, ((sz >> 16) & 0x3FFF) + 1

    def _crop_last_line(self) -> bool:
        w = self._crop_window()
        return w is not None and self._y == w[1] + w[3] - 1

    def _frame_end(self) -> None:
        self._sync_flag(VSYNC)
        was = self._capturing
        self._in_frame = False
        if not was:
            return
        self._complete_capture()

    def _complete_capture(self) -> None:
        """End of the captured frame or crop window: FRAME_RIS (RM0090 15.8.3), snapshot clears CAPTURE."""
        if self._acc:
            n = 4 if self.data_bits() == 8 else 2
            self._push_word(self._acc + [0] * (n - len(self._acc)))
            self._acc = []
            if not self._capturing:                        # the padded word overran: frame lost
                return
        self._capturing = False
        self.frames_captured += 1
        self._flag(FRAME)
        cr = self._cr()
        if cr & CM or self._stop_after:
            self._stop_after = False
            self.set("CR", self._cr() & ~CAPTURE)
            if not cr & CM:
                self._frame_no = 0

    # ------------------------------------------------------------------ DVP hook (camera part)
    def dvp_accepts(self, pin: Any) -> bool:
        """The camera's pixel clock arrives on `pin`: is it muxed to DCMI_PIXCLK (and the block powered)?"""
        if self.absent or self.chip is None:
            return False
        return self.chip.pins.signal_matches(pin.owner, "DCMI_PIXCLK")

    def _data_mask(self, ports: dict) -> tuple[int, int]:
        """(bits carried from the camera, bits forced to 1 by a foreign H pin) for the selected data width."""
        carried, forced, unmuxed = 0, 0, []
        for k in range(self.data_bits()):
            p = self._muxed_pin(f"DCMI_D{k}")
            if p is None:
                unmuxed.append(k)
                continue
            if p.id in ports.get(f"d{k}", ()):
                carried |= 1 << k
            elif p.level() == "H":
                forced |= 1 << k
        if unmuxed:
            self._warn_once(f"nodata{unmuxed}", f"{self.name}: DCMI_D{', D'.join(map(str, unmuxed))} not muxed "
                            f"to a pin: those data bits read 0")
        return carried, forced

    lp_frozen: Any = False              # PWR freezer: Stop / gated Sleep

    def time_shift(self, dns: float, dcycles: int) -> None:
        """Clock back after Stop: camera lines that were due while the clock was off are lost, not replayed late."""
        from ..lowpower import own_events
        for ev in own_events(self, self.dvp_line):
            self.chip.sched.cancel(ev)

    def dvp_line(self, src: Any, t0_ns: float, period_ns: float, samples, ports: dict) -> None:
        """One camera line: `samples[k]` is clocked in at t0 + (k + 1/2) x period (camera clock domain)."""
        if self.absent or self.chip is None or not self._enabled() or self.lp_frozen:
            return                          # Stop: no HCLK, the camera's line is lost
        if self.chip.now_ns() < t0_ns:
            ev = self.chip.sched.at_ns(t0_ns, self.dvp_line, src, t0_ns, period_ns, samples, ports)
            self._evs.append(ev)
            return
        cr = self._cr()
        carried, forced = self._data_mask(ports)
        width = self.data_bits()
        mask = (1 << width) - 1
        vals = [((s & carried) | forced) & mask for s in samples]
        per_word = 4 if width == 8 else 2
        if self._ess(cr):
            if width != 8:
                self._warn_once("ess", f"{self.name}: embedded synchronisation needs 8-bit data (EDM = 00)")
            take = self._embedded(vals)
        else:
            if self._hs_blank or self._vs_blank or not self._in_frame:
                return
            if not self._capturing:
                return
            idx = range(len(vals))
            win = self._crop_window()
            if win is not None:
                hoff, vst, cap, vl = win
                if not vst <= self._y < vst + vl:
                    return
                idx = range(hoff, min(len(vals), hoff + cap))
            take = [(k, vals[k]) for k in idx]
        if not take:
            return
        # group into words; a word completes at the instant its last sample is clocked in
        acc = self._acc
        pending: list[tuple[float, list[int]]] = []
        for k, v in take:
            acc.append(v)
            if len(acc) == per_word:
                pending.append((t0_ns + (k + 0.5) * period_ns, acc))
                acc = []
        self._acc = acc
        now = self.chip.now_ns()
        for t, w in pending:
            if not self._capturing:                          # overrun / sync error: rest of the frame dropped
                break
            if t <= now:
                self._push_word(w)
            else:
                self._evs.append(self.chip.sched.at_ns(t, self._word_due, w))

    def _word_due(self, w: list[int]) -> None:
        self._evs = [e for e in self._evs if not getattr(e, "cancelled", False)]
        if self._enabled() and self._capturing:
            self._push_word(w)

    def _push_word(self, samples: list[int]) -> None:
        if len(samples) == 4:
            word = samples[0] | samples[1] << 8 | samples[2] << 16 | samples[3] << 24
        else:
            word = samples[0] | samples[1] << 16
        if len(self.fifo) >= DEPTH:
            self._flag(OVR)                                  # FIFO reset, frame lost (RM0090 15.5.4 / 15.5.7)
            self._abort_frame()
            if self._cr() & CM:
                self.set("CR", self._cr() & ~CAPTURE)
                self._frame_no = 0
            return
        self.fifo.append(word)
        self.words.append(word)
        self._update()

    # ------------------------------------------------------------------ embedded synchronisation
    def _code_kind(self, xy: int) -> str | None:
        escr, esur = self.get("ESCR"), self.get("ESUR")
        codes = [(escr >> (8 * i)) & 0xFF for i in range(4)]       # FSC, LSC, LEC, FEC
        masks = [(esur >> (8 * i)) & 0xFF for i in range(4)]
        names = ("fs", "ls", "le", "fe")
        for i in (0, 3, 1, 2):                                      # frame codes first
            if i == 0 and codes[0] == 0xFF:
                continue
            if i == 3 and codes[3] == 0xFF:
                continue
            if (xy & masks[i]) == (codes[i] & masks[i]):
                return names[i]
        if codes[3] == 0xFF:
            return "fe"
        return None

    def _embedded(self, vals: list[int]) -> list[tuple[int, int]]:
        """Decode one line of an embedded-sync stream; returns the (index, value) samples to capture."""
        out: list[tuple[int, int]] = []
        n = len(vals)
        i = 0
        fsc_ff = (self.get("ESCR") & 0xFF) == 0xFF
        while i < n:
            if vals[i] == 0xFF and i + 3 < n and vals[i + 1] == 0 and vals[i + 2] == 0:
                kind = self._code_kind(vals[i + 3])
                i += 4
                if kind is None:
                    out = self._sync_error()
                    continue
                if kind == "ls" and fsc_ff and self._e_after_fe and not self._e_inframe:
                    kind = "fs"
                if kind == "fs":
                    if self._e_inline:
                        out = self._sync_error()
                    armed = self._e_after_fe
                    self._e_inframe = True
                    self._e_after_fe = False
                    self._in_frame = True
                    self._vs_blank = False
                    if armed:
                        self._frame_start()
                    else:                                   # no FE seen since ENABLE: frame not captured
                        self._y = -1
                        self._acc = []
                        self._capturing = False
                    self._e_inline = True
                    self._hs_blank = False
                    self._y = 0
                elif kind == "ls":
                    if self._e_inline:
                        out = self._sync_error()
                    self._e_inline = True
                    self._hs_blank = False
                    self._y += 1
                elif kind == "le":
                    if not self._e_inline:
                        out = self._sync_error()
                    self._e_inline = False
                    self._hs_blank = True
                    self._sync_flag(LINE)
                    if self._capturing and self._crop_last_line():
                        self._push_samples(self._crop_embedded(out))     # window complete at this line end
                        out = []
                        self._complete_capture()
                else:                                                   # fe
                    if self._e_inline:
                        self._e_inline = False
                        self._hs_blank = True
                        self._sync_flag(LINE)
                    self._e_inframe = False
                    self._e_after_fe = True
                    self._vs_blank = True
                    self._finish_embedded_frame(out)
                    out = []
                continue
            if self._e_inline and self._e_inframe and self._capturing:
                out.append((i, vals[i]))
            i += 1
        return self._crop_embedded(out)

    def _sync_error(self) -> list:
        """Embedded codes out of order / unknown: ERR_RIS, FIFO reset, wait for a new frame start (15.5.7)."""
        self._flag(ERR)
        if self._capturing:
            self._abort_frame()
        return []

    def _push_samples(self, out: list[tuple[int, int]]) -> None:
        for _k, v in out:
            if not self._capturing:
                return
            self._acc.append(v)
            if len(self._acc) == 4:
                self._push_word(self._acc)
                self._acc = []

    def _finish_embedded_frame(self, out: list[tuple[int, int]]) -> None:
        # samples of the last line are pushed before the frame end flags
        self._push_samples(self._crop_embedded(out))
        self._frame_end()

    def _crop_embedded(self, out: list[tuple[int, int]]) -> list[tuple[int, int]]:
        win = self._crop_window()
        if not out or win is None:
            return out
        hoff, vst, cap, vl = win
        if not vst <= self._y < vst + vl:
            return []
        return out[hoff:hoff + cap]

    # ------------------------------------------------------------------ register hooks
    def write(self, offset: int, size: int, value: int) -> None:
        if self._gate_loc is not None:
            self._gate_ok()
        MMIOBlock.write(self, offset, size, value)

    def _cr_write(self, blk, reg, old, new):
        if self.absent:
            return None
        if new & F42X_BITS:
            self._warn_once("f42x", f"{self.name}: CR bits 16-21 (BSM/OEBS/LSM/OELS) do not exist on the STM32F407 "
                            f"DCMI: ignored")
        if old & new & ENABLE and (new & ~(CAPTURE | ENABLE)) != (old & ~(CAPTURE | ENABLE)):
            self._warn_once("cfg", f"{self.name}: CR configuration changed while ENABLE = 1 (RM0090: configure "
                            f"with ENABLE = 0)")
        if old & ENABLE and not new & ENABLE:
            self._cancel_words()
            self.fifo.clear()
            self._capturing = False
            self._acc = []
            self._stop_after = False
        if new & ENABLE and not old & ENABLE:
            self._sync_from_pins()
            self._e_after_fe = False                      # embedded: capture waits for FE then FS (15.5.3)
            self._e_inframe = self._e_inline = False
        if old & CAPTURE and not new & CAPTURE and self._capturing and not new & CM:
            self._stop_after = True                       # continuous: stop after the current frame
            new |= CAPTURE
        if new & CAPTURE and not old & CAPTURE:
            self._frame_no = 0
        self._store(reg, new)
        self._update()
        return new

    def _sync_from_pins(self) -> None:
        if self.chip is None or self.chip.pins is None:
            return
        p = self._muxed_pin("DCMI_HSYNC")
        self._hs_blank = True if p is None else self._blank("DCMI_HSYNC", p.level(), HSPOL)
        p = self._muxed_pin("DCMI_VSYNC")
        self._vs_blank = True if p is None else self._blank("DCMI_VSYNC", p.level(), VSPOL)
        self._in_frame = False                            # capture begins with the next frame start

    @pure_read
    def _sr_read(self, blk, reg):
        v = 0
        if not self._enabled():
            return SR_FNE if self.fifo else 0               # sync status only while the interface runs
        if self._hs_blank:
            v |= SR_HSYNC
        if self._vs_blank:
            v |= SR_VSYNC
        if self.fifo:
            v |= SR_FNE
        return v

    def _ier_write(self, blk, reg, old, new):
        self._store(reg, new)
        self._update()
        return None

    def _icr_write(self, blk, reg, old, new):
        self.set("RIS", self.get("RIS") & ~(new & 0x1F))
        self._update()
        return 0

    def _dr_read(self, blk, reg):
        if self.absent:
            return 0
        if not self.fifo:
            return 0
        w = self.fifo.popleft()
        self._update()
        return w
