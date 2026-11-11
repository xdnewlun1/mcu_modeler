"""Kinetis I2S/SAI (I2S0, K22 RM ch. 51 and §3.9.7): transmitter and receiver, bit by bit on the pins.

Registers from the SVD (TCSR, TCR1-5, TDR0/1, TFR0/1, TMR and the RX mirror RCSR, RCR1-5, RDR0/1, RFR0/1, RMR at
+0x80, MCR, MDR); behaviour hand-written.  K22 instance (§3.9.7.1): 2 TX and 2 RX data lines, 8-word FIFOs, up to 32
words per frame.

* Audio master clock (MCR/MDR, Figure 5-9): MICS selects the MCLK divider input (0 SYSCLK = core clock, 1 OSC0ERCLK =
  the MCG model's external oscillator, 3 MCGPLLCLK = the MCG PLL output, 2 reserved); MOE=1 enables the fractional
  divider, MCLK = input x (FRACT+1)/(DIVIDE+1), and drives the I2S0_MCLK pin; MOE=0 makes the pin an input
  (`params.mclk_in_hz`, default 0 = no clock).  MICS cannot change while MOE=1 (the write keeps the old value);
  an MDR write while MOE=1 sets MCR.DUF for two MCLK periods, during which further MDR writes are ignored.
  The MCLK pin toggles edge by edge only up to `params.mclk_edges_max_hz` (default 1 MHz); a faster MCLK sets the
  pin high and logs one `info` with its frequency (the CPU-side model is unaffected).
* Bit clock: TCR2/RCR2.BCD=1 generates it from MSEL (00 bus clock, 01 MCLK; 10/11 are not connected on the K22:
  one `warn`, no clock) divided by (DIV+1) x 2; BCD=0 takes it from the I2Sx_TX_BCLK / RX_BCLK pin (slave).  It runs
  while TCSR.BCE (set with TE) is set.  BCP=0: outputs change on the rising edge, inputs are sampled on the falling
  edge; BCP=1 the opposite (I2S).  TE/RE cleared: the side stops at the end of the current frame (TE/RE read 1 until
  then).  Synchronous mode (TCR2.SYNC=01 / RCR2.SYNC=01, 51.4.3): the synchronous side uses the other side's bit
  clock and frame sync and runs only while both are enabled; synchronous with another SAI (10/11) has no partner on
  the K22 (one `warn`).
* Frames (51.4.4): FRSZ+1 words, word 0 W0W+1 bits, the others WNW+1; MF selects MSB first; FBT is the bit index of
  the first bit shifted (data aligned anywhere in the 32-bit FIFO word).  Frame sync generated (FSD=1) for SYWD+1
  bits from the first bit (FSE=1: one bit earlier), active high or low (FSP); frames follow back to back; no frame
  sync during the first four bit clocks after enabling.  External frame sync (FSD=0): the FS pin is read at every
  drive edge; a new assertion starts a frame (FSE=0: this bit, FSE=1: the next one); one that arrives while a frame
  is still running is ignored and sets SEF.
* FIFOs: 8 x 32 bits per channel (TCR3.TCE / RCR3.RCE enable channels 0/1; TDR/RDR accesses of disabled channels
  are ignored).  TX: an unmasked word pops one entry per enabled channel when it starts; an empty FIFO sets FEF and
  the channel repeats the last valid word until FEF is cleared and the next frame starts.  RX: the word enters the
  FIFO after its last bit; a full FIFO sets FEF and received data is discarded until FEF is cleared and a frame
  starts.  TFR/RFR show WFP/RFP (4-bit pointers, MSB = wrap).  TMR/RMR are double buffered (latched when TE/RE is
  first set and at every frame end); a masked TX word tristates TXDn and reads nothing.
* Flags (TCSR/RCSR): FRF (TX: entries <= TFW in any enabled FIFO; RX: entries > RFW), FWF (TX empty / RX full),
  FEF, SEF, WSF (set at the second bit of word WDFL) - the last three w1c.  FR resets the FIFO pointers, SR holds
  the side in reset (status cleared) while set.  IRQ "I2S0_Tx" / "I2S0_Rx" = OR of the flags with xxIE; DMA
  requests (DMAMUX sources 13 = transmit, 12 = receive, §3.3.9.1) while FRF & FRDE or FWF & FWDE.
* Pins (PORT mux, signal table + direction hook): BCLK/FS outputs while generated internally, else inputs; TXDn is an
  output while its word is transmitted (tristated = input otherwise), RXDn inputs; MCLK out while MOE=1.  Levels
  are read from the pins muxed to I2S0_RXDn (X = 0).  When no pin is muxed to a side's bit clock and both clock and
  frame sync are internal, bits are not scheduled one by one: one event per word keeps the same timing.
* Observation: `tx_words[ch]` / `rx_words[ch]` (deques of (t_ns, word)), and an `i2s` log event per
  `params.event_frames` frames (default 64) and side: `{block, dir: tx|rx, ch, frames, words: [first word of each
  slot ...], bits}` (docs/CONTRACTS.md).
* Stop modes (`clock_stop`, 51.1.3.2 / 3.9.7.3): a side with xCSR.STOPE = 1 whose bit clock survives the stop (external,
  or from an MCLK on the pin / OSCERCLK) keeps running and its interrupts wake the core; other sides finish the current
  frame and stay disabled until the wake-up (LLS/VLLS: always); the MCLK output stops unless it comes from OSCERCLK.
Not modelled: BCS (bit clock swap; one `warn`), BCI, Debug-mode behaviour (DBGE), the MCLK duty cycle
and divider jitter (the average frequency is exact), pad delays.
"""
from __future__ import annotations

from collections import deque
from typing import Any

from ...pins import PinBoard
from ..registry import register
from ._util import GatedBlock, build_regs, find_model, irq_number
from .clockutil import bus_hz
from .dma import dma_request

TE, STOPE, DBGE, BCE, FR, SRST = 1 << 31, 1 << 30, 1 << 29, 1 << 28, 1 << 25, 1 << 24
WSF, SEF, FEF, FWF, FRF = 1 << 20, 1 << 19, 1 << 18, 1 << 17, 1 << 16
WSIE, SEIE, FEIE, FWIE, FRIE, FWDE, FRDE = 1 << 12, 1 << 11, 1 << 10, 1 << 9, 1 << 8, 2, 1
W1C = WSF | SEF | FEF
DEPTH = 8
MOE, DUF = 1 << 30, 1 << 31


class _Side:
    """Transmitter (tx=True) or receiver of the SAI."""

    def __init__(self, sai: "SAI", tx: bool):
        self.sai = sai
        self.tx = tx
        p = "T" if tx else "R"
        self.p = p
        self.dir = "tx" if tx else "rx"
        self.csr, self.cr = f"{p}CSR", [None] + [f"{p}CR{i}" for i in range(1, 6)]
        self.mr = f"{p}MR"
        s = "TX" if tx else "RX"
        self.sig_bclk, self.sig_fs = f"I2S0_{s}_BCLK", f"I2S0_{s}_FS"
        self.sig_data = [f"I2S0_{'TXD' if tx else 'RXD'}{n}" for n in range(2)]
        self.fifo = [deque(), deque()]
        self.wfp = [0, 0]
        self.rfp = [0, 0]
        self.words: list[deque] = [deque(maxlen=100_000), deque(maxlen=100_000)]
        self.irqn: int | None = None
        self.dma_src = -1
        self._line = False
        self.init()

    def init(self) -> None:
        self.running = False            # bit clock / frame engine active
        self.enabled = False            # TE/RE seen set (TE stays 1 until the frame ends after a clear)
        self.ev = None
        self.p_bit = -4                 # bit position in the current frame (negative: start-up / idle)
        self.in_frame = False
        self.total = 0
        self.layout: list[tuple[int, int, int]] = []   # per bit: (word, bit in word, word width)
        self.mask_latched = 0
        self.cur = [0, 0]               # word being shifted per channel
        self.last = [0, 0]              # last valid TX word (FEF repeat)
        self.err = False                # FEF latched: TX repeats / RX discards until cleared + next frame
        self.fs_prev = False
        self.fs_pending = False         # external FSE=1: bit 0 at the next drive edge
        self.bclk = None                # level of the bit clock pin (external)
        self.tri = [True, True]         # TXDn tristated
        self.frames = 0
        self.block: list = []
        self.t_half = 0.0
        self.t_next = 0.0
        self.word_mode = False
        self.te_clear = False           # TE/RE cleared by software: reads 1 until the end of the frame
        self.bce_clear = False
        self.lp_off = False             # Stop mode without STOPE / clock: disabled from the end of the frame

    # ------------------------------------------------------------------ config helpers
    def g(self, i: int) -> int:
        return self.sai.get(self.cr[i])

    def csr_v(self) -> int:
        return self.sai.get(self.csr)

    def chans(self) -> int:
        return (self.g(3) >> 16) & 3

    def sync(self) -> int:
        return (self.g(2) >> 30) & 3

    def widths(self) -> list[int]:
        cr4, cr5 = self.g(4), self.g(5)
        n = ((cr4 >> 16) & 0x1F) + 1
        w0, wn = ((cr5 >> 16) & 0x1F) + 1, ((cr5 >> 24) & 0x1F) + 1
        return [w0] + [wn] * (n - 1)

    def build_layout(self) -> None:
        lay = []
        for w, wid in enumerate(self.widths()):
            for b in range(wid):
                lay.append((w, b, wid))
        self.layout = lay
        self.total = len(lay)

    def bit_index(self, b: int) -> int:
        fbt = (self.g(5) >> 8) & 0x1F
        return fbt - b if self.g(4) & 0x10 else fbt + b

    def bclk_hz(self) -> float:
        """Internally generated bit clock frequency (0 if none)."""
        cr2 = self.g(2)
        msel = (cr2 >> 26) & 3
        if msel == 0:
            src = bus_hz(self.sai.chip) if self.sai.chip is not None else 0.0
        elif msel == 1:
            src = self.sai.mclk_hz()
        else:
            self.sai._warn_once(f"msel{msel}", f"I2S0: {self.p}CR2.MSEL={msel} selects an MCLK option that is not "
                                f"connected on the K22: no bit clock")
            src = 0.0
        return src / (((cr2 & 0xFF) + 1) * 2)

    # ------------------------------------------------------------------ FIFO / flags
    def count(self, ch: int) -> int:
        return len(self.fifo[ch])

    def flags(self) -> int:
        ch = [c for c in (0, 1) if self.chans() >> c & 1]
        v = 0
        if ch:
            wm = self.g(1) & 7
            if self.tx:
                if any(self.count(c) <= wm for c in ch):
                    v |= FRF
                if any(self.count(c) == 0 for c in ch):
                    v |= FWF
            else:
                if any(self.count(c) > wm for c in ch):
                    v |= FRF
                if any(self.count(c) >= DEPTH for c in ch):
                    v |= FWF
        return v

    def push(self, ch: int, word: int) -> bool:
        if len(self.fifo[ch]) >= DEPTH:
            return False
        self.fifo[ch].append(word & 0xFFFFFFFF)
        self.wfp[ch] = (self.wfp[ch] + 1) & 0xF
        return True

    def pop(self, ch: int) -> int | None:
        if not self.fifo[ch]:
            return None
        self.rfp[ch] = (self.rfp[ch] + 1) & 0xF
        return self.fifo[ch].popleft()

    def fifo_reset(self) -> None:
        for c in (0, 1):
            self.fifo[c].clear()
            if self.tx:
                self.wfp[c] = self.rfp[c]
            else:
                self.rfp[c] = self.wfp[c]


@register("kinetis.sai")
class SAI(GatedBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.gate = "I2S0"
        self.tx = _Side(self, True)
        self.rx = _Side(self, False)
        ov: dict[str, dict] = {"MCR": {"ro_mask": DUF, "on_write": self._mcr_write},
                               "MDR": {"on_write": self._mdr_write}}
        for sd in (self.tx, self.rx):
            ov[sd.csr] = {"w1c_mask": W1C, "ro_mask": FWF | FRF, "on_read": self._csr_read(sd),
                          "on_write": self._csr_write(sd)}
            ov[sd.cr[1]] = {"on_write": self._cfg_write(sd)}
            for i in range(2, 6):
                ov[sd.cr[i]] = {"on_write": self._cfg_write(sd)}
            for c in (0, 1):
                ov[f"{sd.p}FR{c}"] = {"ro_mask": 0xFFFFFFFF, "on_read": self._fr_read(sd, c)}
        for c in (0, 1):
            ov[f"TDR{c}"] = {"on_read": lambda b, r: 0, "on_write": self._tdr_write(c)}
            ov[f"RDR{c}"] = {"ro_mask": 0xFFFFFFFF, "on_read": self._rdr_read(c)}
        r = build_regs((name, "I2S0"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.mclk_in_hz = float(params.get("mclk_in_hz", 0.0))
        self.mclk_edges_max_hz = float(params.get("mclk_edges_max_hz", 1e6))
        self.event_frames = int(params.get("event_frames", 64))
        self._warned: set[str] = set()
        self._busy = False
        self._mclk_ev = None
        self._duf_ev = None
        self._mclk_lvl: str | None = None
        self._mcg: Any = None
        self._listen: set = set()

    # ------------------------------------------------------------------ setup
    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._mcg = find_model(chip, "kinetis.mcg", "MCG")
        self.tx.irqn = irq_number(chip, "I2S0_Tx", f"{self.name}_Tx")
        self.rx.irqn = irq_number(chip, "I2S0_Rx", f"{self.name}_Rx")
        self.tx.dma_src = int(self.params.get("dma_tx", 13))
        self.rx.dma_src = int(self.params.get("dma_rx", 12))
        for b in chip.peripherals.values():
            if getattr(b, "model", None) == "kinetis.dmamux":
                b.register_source(self.tx.dma_src, lambda: self._dma_active(self.tx))
                b.register_source(self.rx.dma_src, lambda: self._dma_active(self.rx))
            if getattr(b, "model", None) == "kinetis.port":
                hooks = getattr(b, "signal_dir_hooks", None)
                if hooks is not None and self._pin_dir not in hooks:
                    hooks.append(self._pin_dir)
        board = getattr(chip, "pins", None)
        if board is not None:
            for sd in (self.tx, self.rx):
                for p in board.signal_pins(sd.sig_bclk):
                    if (sd.dir, p.id) not in self._listen:
                        self._listen.add((sd.dir, p.id))
                        board.input_listeners[p.id].append(lambda pin, _sd=sd: self._bclk_pin(_sd, pin))

    def reset(self) -> None:
        chip = self.chip
        for sd in (getattr(self, "tx", None), getattr(self, "rx", None)):
            if sd is None:
                continue
            if chip is not None:
                chip.sched.cancel(sd.ev)
                if sd._line and sd.irqn is not None:
                    self.irq(sd.irqn, False)
            sd._line = False
            for c in (0, 1):
                sd.fifo[c].clear()
                sd.words[c].clear()
            sd.wfp, sd.rfp = [0, 0], [0, 0]
            sd.init()
        if chip is not None:
            chip.sched.cancel(getattr(self, "_mclk_ev", None))
            chip.sched.cancel(getattr(self, "_duf_ev", None))
        self._mclk_ev = self._duf_ev = None
        self._mclk_lvl = None
        self._warned = set()
        super().reset()
        if chip is not None and chip.pins is not None:
            for s in self._all_signals():
                chip.pins.release_signal(s)
            self._refresh_ports()

    def _all_signals(self) -> list[str]:
        out = ["I2S0_MCLK"]
        for sd in (self.tx, self.rx):
            out += [sd.sig_bclk, sd.sig_fs, *sd.sig_data]
        return out

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=msg)

    # ------------------------------------------------------------------ clocks
    def mclk_input_hz(self) -> float:
        mics = (self.get("MCR") >> 24) & 3
        chip = self.chip
        if chip is None:
            return 0.0
        if mics == 0:
            return float(chip.clock.hz)
        if mics == 1:
            return float(getattr(self._mcg, "ext_hz", 0.0) or 0.0)
        if mics == 3:
            return float(self._mcg.pll_hz()) if self._mcg is not None else 0.0
        self._warn_once("mics2", "I2S0: MCR.MICS=2 is reserved on the K22: no MCLK")
        return 0.0

    def mclk_hz(self) -> float:
        """Audio master clock seen by the bit clock dividers."""
        if not self.get("MCR") & MOE:
            return self.mclk_in_hz
        mdr = self.get("MDR")
        fract, divide = (mdr >> 12) & 0xFF, mdr & 0xFFF
        if fract > divide:
            self._warn_once("fract", "I2S0: MDR.FRACT > DIVIDE is not supported (51.3.20)")
        return self.mclk_input_hz() * (fract + 1) / (divide + 1)

    def _mclk_update(self) -> None:
        chip = self.chip
        if chip is None:
            return
        chip.sched.cancel(self._mclk_ev)
        self._mclk_ev = None
        if not self.get("MCR") & MOE:
            if self._mclk_lvl is not None:
                self._mclk_lvl = None
                if chip.pins is not None:
                    chip.pins.release_signal("I2S0_MCLK")
            self._refresh_ports()
            return
        hz = self.mclk_hz()
        first = self._mclk_lvl is None
        if hz <= 0:
            self._set_mclk("L")
        elif hz > self.mclk_edges_max_hz:
            self._set_mclk("H")
            self.emit("info", block=self.name, msg=f"I2S0: MCLK {hz / 1e6:.6f} MHz on I2S0_MCLK (faster than "
                      f"{self.mclk_edges_max_hz / 1e6:g} MHz: not toggled edge by edge)")
        else:
            self._set_mclk("L")
            self._mclk_ev = chip.sched.at_ns(chip.now_ns() + 0.5e9 / hz, self._mclk_tick, chip.now_ns() + 0.5e9 / hz,
                                             0.5e9 / hz)
        if first:
            self._refresh_ports()

    # ------------------------------------------------------------------ low-power modes (SMC)
    def _clock_survives_stop(self, cs: _Side) -> bool:
        """The bit clock of clock side `cs` keeps running in Stop/VLPS (51.1.3.2): external (BCD=0), or divided from
        an audio master clock that keeps running (MCLK pin input with MOE=0, or OSCERCLK, MICS=1)."""
        if not cs.g(2) & (1 << 24):
            return True
        if (cs.g(2) >> 26) & 3 != 1:                      # MSEL: bus clock (stopped)
            return False
        return not self.get("MCR") & MOE or (self.get("MCR") >> 24) & 3 == 1

    def clock_stop(self, stopped: bool, mode: str) -> None:
        """Stop / VLPS (51.1.3.2, 3.9.7.3): a side keeps operating with xCSR.STOPE = 1 and a bit clock that survives
        the stop (an interrupt from it wakes the core through the NVIC); otherwise it is disabled after completing the
        current frame.  LLS/VLLS (3.9.7.3.2): STOPE is ignored, both sides stop at the end of the frame.  The MCLK
        output stops unless it comes from OSCERCLK.  WAIT/VLPW: unaffected."""
        if self.chip is None or mode in ("WAIT", "VLPW"):
            return
        if stopped:
            low_leak = mode in ("LLS", "VLLS")
            for sd in (self.tx, self.rx):
                cs = self._partner(sd) if sd.sync() == 1 else sd
                keep = not low_leak and bool(sd.csr_v() & STOPE) and self._clock_survives_stop(cs)
                sd.lp_off = not keep
            if low_leak or (self.get("MCR") >> 24) & 3 != 1:
                self.chip.sched.cancel(self._mclk_ev)      # MCLK divider input stopped: the pin holds its level
                self._mclk_ev = None
            self._reconsider()
            for cs in (self.tx, self.rx):
                if cs.running and not cs.in_frame and cs.lp_off and not self._served(cs):
                    self._stop_clock(cs)
            return
        self.tx.lp_off = self.rx.lp_off = False
        if self.get("MCR") & MOE and self._mclk_ev is None and 0 < self.mclk_hz() <= self.mclk_edges_max_hz:
            self._mclk_update()
        self._reconsider()

    def _set_mclk(self, lvl: str) -> None:
        self._mclk_lvl = lvl
        pins = getattr(self.chip, "pins", None)
        if pins is not None and pins.signal_pins("I2S0_MCLK"):
            pins.drive_signal("I2S0_MCLK", lvl)

    def _mclk_tick(self, t: float, half: float) -> None:
        self._set_mclk("L" if self._mclk_lvl == "H" else "H")
        self._mclk_ev = self.chip.sched.at_ns(t + half, self._mclk_tick, t + half, half)

    # ------------------------------------------------------------------ pins
    def _pin_dir(self, owner: str) -> tuple[str, Any] | None:
        if "I2S0_" not in owner:
            return None
        if PinBoard.signal_matches(owner, "I2S0_MCLK"):
            return ("out", self._mclk_lvl or "L") if self.get("MCR") & MOE else ("in", None)
        for sd in (self.tx, self.rx):
            if PinBoard.signal_matches(owner, sd.sig_bclk):
                idle = "H" if sd.g(2) & (1 << 25) else "L"
                return ("out", idle) if self._drives_clock(sd, "bclk") else ("in", None)
            if PinBoard.signal_matches(owner, sd.sig_fs):
                idle = "H" if sd.g(4) & 2 else "L"
                return ("out", idle) if self._drives_clock(sd, "fs") else ("in", None)
            for c in (0, 1):
                if PinBoard.signal_matches(owner, sd.sig_data[c]):
                    if sd.tx and not sd.tri[c]:
                        return "out", "L"
                    return "in", None
        return None

    def _drives_clock(self, sd: _Side, what: str) -> bool:
        """This side generates its own BCLK / FS on its pins (asynchronous, BCD/FSD=1, clock running)."""
        if sd.sync():
            return False
        if what == "bclk":
            return bool(sd.g(2) & (1 << 24)) and bool(sd.csr_v() & BCE)
        return bool(sd.g(4) & 1) and sd.running

    def _refresh_ports(self) -> None:
        chip = self.chip
        if chip is None or chip.pins is None:
            return
        for b in chip.peripherals.values():
            f = getattr(b, "refresh_function", None)
            if f is not None and getattr(b, "model", None) == "kinetis.port":
                f(lambda owner: "I2S0_" in str(owner))

    def _drive(self, sig: str, lvl: str) -> None:
        pins = getattr(self.chip, "pins", None)
        if pins is not None and pins.signal_pins(sig):
            pins.drive_signal(sig, lvl)

    def _muxed(self, sig: str) -> bool:
        pins = getattr(self.chip, "pins", None)
        if pins is None:
            return False
        return any(pins.signal_matches(p.owner, sig) for p in pins.signal_pins(sig))

    def _level(self, sig: str) -> str | None:
        pins = getattr(self.chip, "pins", None)
        if pins is None:
            return None
        for p in pins.signal_pins(sig):
            if pins.signal_matches(p.owner, sig):
                return p.level()
        return None

    # ------------------------------------------------------------------ engine control
    def _partner(self, sd: _Side) -> _Side:
        return self.rx if sd.tx else self.tx

    def _side_active(self, sd: _Side) -> bool:
        """TE/RE set (and, synchronous, the partner's too), not in software reset."""
        v = sd.csr_v()
        if not v & TE or v & SRST or sd.sync() in (2, 3) or sd.lp_off:
            return False
        if sd.sync() == 1:
            return bool(self._partner(sd).csr_v() & TE)
        return True

    def _served(self, cs: _Side) -> list[_Side]:
        """Enabled sides clocked by clock side `cs`: itself and a synchronous partner."""
        out = [cs] if cs.enabled else []
        o = self._partner(cs)
        if o.sync() == 1 and o.enabled:
            out.append(o)
        return out

    def _want_frames(self, cs: _Side) -> bool:
        return any(not sd.te_clear for sd in self._served(cs))

    def _reconsider(self) -> None:
        """Start / stop the sides and bit clocks after an enable or configuration change."""
        for sd in (self.tx, self.rx):
            if sd.sync() in (2, 3):
                self._warn_once(f"sync{sd.p}", f"I2S0: {sd.p}CR2.SYNC={sd.sync()} (synchronous with another SAI): "
                                f"the K22 has a single SAI, the side does not run")
            if sd.g(2) & (1 << 29):
                self._warn_once("bcs", "I2S0: bit clock swap (xCR2.BCS) is not modelled")
            cs = self._partner(sd) if sd.sync() == 1 else sd
            active = self._side_active(sd)
            if active and not sd.enabled:
                sd.enabled = True
                sd.mask_latched = self.get(sd.mr)
                sd.err = False
                sd.build_layout()
                if not cs.in_frame:                      # no frame sync during 4 bit clocks after enabling
                    cs.p_bit = -5 - (1 if cs.g(4) & 8 else 0)
            elif not active and sd.enabled and not cs.in_frame:
                self._disable(sd)
        for cs in (self.tx, self.rx):
            if cs.sync():
                if cs.running:
                    self._stop_clock(cs)
                continue
            want = bool(cs.csr_v() & BCE) or bool(self._served(cs))
            if want and not cs.running:
                self._start_clock(cs)
            elif not want and cs.running and not cs.in_frame:
                self._stop_clock(cs)
        self._refresh_ports()
        self._update(self.tx)
        self._update(self.rx)

    def _disable(self, sd: _Side) -> None:
        sd.enabled = False
        if sd.te_clear:
            sd.te_clear = False
            self.set(sd.csr, self.get(sd.csr) & ~TE)
        if sd.tx and not all(sd.tri):
            sd.tri = [True, True]
            self._refresh_ports()

    def _start_clock(self, cs: _Side) -> None:
        chip = self.chip
        if chip is None:
            return
        cs.running = True
        cs.in_frame = False
        cs.fs_prev = False
        cs.fs_pending = False
        if not cs.g(2) & (1 << 24):                      # BCD=0: edges come from the pin
            cs.bclk = self._level(cs.sig_bclk)
            return
        hz = cs.bclk_hz()
        if hz <= 0:
            self._warn_once(f"noclk{cs.p}", f"I2S0: the {cs.dir.upper()} bit clock has no source clock (MSEL / MCLK):"
                            f" idle")
            cs.running = False
            return
        bcp = bool(cs.g(2) & (1 << 25))
        cs.t_half = 0.5e9 / hz
        self._drive(cs.sig_bclk, "H" if bcp else "L")    # idle level until the first drive edge
        if cs.g(4) & 1:
            self._drive(cs.sig_fs, "H" if cs.g(4) & 2 else "L")
        sigs = [cs.sig_bclk, cs.sig_fs, *cs.sig_data]
        o = self._partner(cs)
        if o.sync() == 1:
            sigs += o.sig_data
        cs.word_mode = bool(cs.g(4) & 1) and not any(self._muxed(s) for s in sigs)
        cs.t_next = chip.now_ns() + cs.t_half
        cs.ev = chip.sched.at_ns(cs.t_next, self._tick, cs, True)

    def _stop_clock(self, cs: _Side) -> None:
        if self.chip is not None:
            self.chip.sched.cancel(cs.ev)
        cs.ev = None
        cs.running = False
        cs.in_frame = False

    # ------------------------------------------------------------------ bit engine
    def _tick(self, cs: _Side, drive: bool) -> None:
        """Internal bit clock edge of clock side `cs`."""
        cs.ev = None
        if not cs.running:
            return
        bcp = bool(cs.g(2) & (1 << 25))
        if cs.word_mode:
            n = self._word_fast(cs)
            if cs.running:
                cs.t_next += n * 2 * cs.t_half
                cs.ev = self.chip.sched.at_ns(cs.t_next, self._tick, cs, True)
            return
        if drive:
            self._drive_edge(cs)
            self._drive(cs.sig_bclk, "L" if bcp else "H")
        else:
            self._drive(cs.sig_bclk, "H" if bcp else "L")
            self._sample_edge(cs)
        if cs.running:
            cs.t_next += cs.t_half
            cs.ev = self.chip.sched.at_ns(cs.t_next, self._tick, cs, not drive)

    def _word_fast(self, cs: _Side) -> int:
        """No pin watches this side: run the bits of one word (or one idle bit) in one event."""
        n = 0
        while True:
            self._drive_edge(cs)
            self._sample_edge(cs)
            n += 1
            if not cs.running or not cs.in_frame:
                return n
            nxt = cs.p_bit + 1
            if nxt >= cs.total or cs.layout[nxt][0] != cs.layout[cs.p_bit][0]:
                return n

    def _bclk_pin(self, sd: _Side, pin: Any) -> None:
        """External bit clock edge on the pin muxed to this side's BCLK (BCD=0)."""
        if not sd.running or sd.sync() or sd.g(2) & (1 << 24):
            return
        if not PinBoard.signal_matches(pin.owner, sd.sig_bclk):
            return
        lvl = pin.level()
        prev, sd.bclk = sd.bclk, lvl
        if lvl == prev or lvl not in ("H", "L") or prev not in ("H", "L"):
            return
        if (lvl == "H") != bool(sd.g(2) & (1 << 25)):
            self._drive_edge(sd)
        else:
            self._sample_edge(sd)

    def _drive_edge(self, cs: _Side) -> None:
        """Start of a bit of clock side `cs`: frame boundaries, frame sync, TX data out."""
        cr4 = cs.g(4)
        fse = bool(cr4 & 8)
        cs.p_bit += 1
        if cr4 & 1:                                      # internal frame sync
            if cs.in_frame and cs.p_bit >= cs.total:
                self._frame_end(cs)
                if self._want_frames(cs):
                    self._frame_begin(cs)
            elif not cs.in_frame and cs.p_bit >= 0 and self._want_frames(cs):
                self._frame_begin(cs)
            sywd = (cr4 >> 8) & 0x1F
            if cs.in_frame:
                last = cs.p_bit == cs.total - 1
                active = (cs.p_bit + (1 if fse else 0) <= sywd) or (fse and last and self._want_frames(cs)
                                                                     and not self._ends_after(cs))
            else:
                active = fse and cs.p_bit == -1 and self._want_frames(cs)
            self._drive(cs.sig_fs, ("L" if active else "H") if cr4 & 2 else ("H" if active else "L"))
        else:                                            # external frame sync, read at every drive edge
            lvl = self._level(cs.sig_fs)
            act = (lvl == "H") != bool(cr4 & 2)
            rising = act and not cs.fs_prev
            cs.fs_prev = act
            if cs.in_frame and cs.p_bit >= cs.total:
                self._frame_end(cs)
            if cs.fs_pending:
                cs.fs_pending = False
                if not cs.in_frame and self._want_frames(cs):
                    self._frame_begin(cs)
            if rising:
                if cs.in_frame and not (fse and cs.p_bit == cs.total - 1):
                    for sd in self._served(cs):          # still busy with the previous frame: ignored
                        self._set_flag(sd, SEF)
                elif fse or cs.in_frame:
                    cs.fs_pending = True
                elif self._want_frames(cs):
                    self._frame_begin(cs)
        if cs.in_frame:
            for sd in self._served(cs):
                if cs.p_bit < sd.total:
                    w, b, _wid = sd.layout[cs.p_bit]
                    if b == 1 and w == (sd.g(3) & 0x1F):
                        self._set_flag(sd, WSF)          # start of the second bit of word WDFL
                    if sd.tx:
                        self._tx_bit(sd, cs.p_bit)

    def _ends_after(self, cs: _Side) -> bool:
        """All served sides were disabled by software: no frame follows the current one."""
        return all(sd.te_clear for sd in self._served(cs))

    def _sample_edge(self, cs: _Side) -> None:
        if not cs.in_frame:
            return
        for sd in self._served(cs):
            if not sd.tx:
                self._rx_bit(sd, cs.p_bit)

    def _frame_begin(self, cs: _Side) -> None:
        cs.in_frame = True
        cs.p_bit = 0
        for sd in self._served(cs):
            sd.build_layout()
            if sd.err and not self.get(sd.csr) & FEF:
                sd.err = False                           # FEF cleared: back to normal at a frame start

    def _frame_end(self, cs: _Side) -> None:
        cs.in_frame = False
        for sd in self._served(cs):
            sd.mask_latched = self.get(sd.mr)
            sd.frames += 1
            if self.event_frames and sd.frames % self.event_frames == 0:
                self._emit_block(sd)
            if sd.te_clear or not self._side_active(sd):
                self._disable(sd)
        cs.p_bit = -1
        if cs.bce_clear:
            cs.bce_clear = False
            self.set(cs.csr, self.get(cs.csr) & ~BCE)
        if (not cs.csr_v() & BCE or cs.lp_off) and not self._served(cs):
            self._stop_clock(cs)

    def _emit_block(self, sd: _Side) -> None:
        words, sd.block = sd.block, []
        head = " ".join(f"{w:X}" for w in words[:8])
        self.emit("i2s", block=self.name, dir=sd.dir, frames=self.event_frames, words=words, bits=sd.widths()[0],
                  msg=f"{self.name} {sd.dir}: {self.event_frames} frames, ch0 {head}{' ...' if len(words) > 8 else ''}")

    def _tx_bit(self, sd: _Side, p: int) -> None:
        w, b, _wid = sd.layout[p]
        if b == 0:
            masked = bool(sd.mask_latched >> w & 1)
            chans = sd.chans()
            changed = False
            for c in (0, 1):
                tri = masked or not chans >> c & 1
                if tri != sd.tri[c]:
                    sd.tri[c] = tri
                    changed = True
                if tri:
                    continue
                if sd.err:
                    word = sd.last[c]                    # underrun: repeat the last valid word
                else:
                    v = sd.pop(c)
                    if v is None:
                        sd.err = True
                        self._set_flag(sd, FEF, update=False)
                        word = sd.last[c]
                    else:
                        word = sd.last[c] = v
                sd.cur[c] = word
                sd.words[c].append((self.now_ns(), word))
                if c == 0 and len(sd.block) < 64:
                    sd.block.append(word)
            if changed:
                self._refresh_ports()
            self._update(sd)
        idx = sd.bit_index(b)
        for c in (0, 1):
            if not sd.tri[c]:
                self._drive(sd.sig_data[c], "H" if 0 <= idx < 32 and sd.cur[c] >> idx & 1 else "L")

    def _rx_bit(self, sd: _Side, p: int) -> None:
        if p >= sd.total:
            return
        w, b, wid = sd.layout[p]
        if b == 0:
            sd.cur = [0, 0]
        idx = sd.bit_index(b)
        chans = sd.chans()
        for c in (0, 1):
            if chans >> c & 1 and 0 <= idx < 32 and self._level(sd.sig_data[c]) == "H":
                sd.cur[c] |= 1 << idx
        if b != wid - 1 or sd.mask_latched >> w & 1 or sd.err:
            return
        for c in (0, 1):
            if not chans >> c & 1:
                continue
            word = sd.cur[c]
            sd.words[c].append((self.now_ns(), word))
            if c == 0 and len(sd.block) < 64:
                sd.block.append(word)
            if not sd.push(c, word):
                sd.err = True                            # overflow: discard until FEF is cleared
                self._set_flag(sd, FEF, update=False)
        self._update(sd)

    # ------------------------------------------------------------------ flags / IRQ / DMA
    def _set_flag(self, sd: _Side, f: int, update: bool = True) -> None:
        self.set(sd.csr, self.get(sd.csr) | f)
        if update:
            self._update(sd)

    def _status(self, sd: _Side) -> int:
        v = self.get(sd.csr) & ~(FWF | FRF)
        if sd.csr_v() & SRST:
            return v
        return v | sd.flags()

    def _dma_active(self, sd: _Side) -> bool:
        st = self._status(sd)
        return bool((st & FRF and st & FRDE) or (st & FWF and st & FWDE))

    def _update(self, sd: _Side) -> None:
        if self.chip is None:
            return
        st = self._status(sd)
        line = bool((st >> 8) & (st >> 16) & 0x1F)
        if line != sd._line and sd.irqn is not None:
            sd._line = line
            self.irq(sd.irqn, line)
        if not self._busy and self._dma_active(sd):
            self._busy = True
            try:
                dma_request(self.chip, sd.dma_src)
            finally:
                self._busy = False

    # ------------------------------------------------------------------ register hooks
    def _csr_read(self, sd: _Side):
        def rd(blk, reg):
            return self._status(sd) & ~FR
        return rd

    def _csr_write(self, sd: _Side):
        def wr(blk, reg, old, new):
            v = new
            if v & FR:
                sd.fifo_reset()
                v &= ~FR
            if v & SRST:
                sd.fifo_reset()
                v &= ~W1C
                sd.err = False
            if v & TE:
                v |= BCE                                 # BCE follows TE (51.3.1)
                sd.te_clear = False
            cs = self._partner(sd) if sd.sync() == 1 else sd
            busy = cs.in_frame and sd.enabled
            if old & TE and not v & TE and busy:
                sd.te_clear = True                       # TE reads 1 until the end of the current frame
                v |= TE
            if old & BCE and not v & BCE and cs is sd and sd.running and sd.in_frame:
                sd.bce_clear = True
                v |= BCE
            self._store(reg, v)
            self._reconsider()
            return v
        return wr

    def _cfg_write(self, sd: _Side):
        def wr(blk, reg, old, new):
            if sd.enabled and reg.name != sd.cr[1] and old != new:
                self._warn_once(f"cfg{reg.name}", f"I2S0: {reg.name} written while {sd.p}E is set (51.3: must not "
                                f"be altered)")
            self._update(sd)
            return None
        return wr

    def _fr_read(self, sd: _Side, c: int):
        def rd(blk, reg):
            wfp = (sd.rfp[c] + len(sd.fifo[c])) & 0xF if sd.tx else sd.wfp[c]
            rfp = sd.rfp[c] if sd.tx else (sd.wfp[c] - len(sd.fifo[c])) & 0xF
            return (wfp << 16) | rfp
        return rd

    def _tdr_write(self, c: int):
        def wr(blk, reg, old, new):
            sd = self.tx
            if sd.chans() >> c & 1:
                sd.push(c, new)
                self._update(sd)
            return 0
        return wr

    def _rdr_read(self, c: int):
        def rd(blk, reg):
            sd = self.rx
            if not sd.chans() >> c & 1:
                return 0
            v = sd.pop(c)
            self._update(sd)
            return v if v is not None else 0
        return rd

    def _mcr_write(self, blk, reg, old, new):
        v = new & ~DUF
        if old & MOE and (old ^ new) & (3 << 24):
            v = (v & ~(3 << 24)) | (old & (3 << 24))   # MICS fixed while the divider runs
            self._warn_once("mics", "I2S0: MCR.MICS written while MOE=1: ignored (51.3.19)")
        self._store(reg, v | (old & DUF))
        if (old ^ v) & MOE:
            self._mclk_update()
        self._reconsider()
        return None

    def _mdr_write(self, blk, reg, old, new):
        mcr = self.get("MCR")
        if mcr & DUF:
            return old                                   # blocked while an update is in progress
        if mcr & MOE and self.chip is not None:
            self.set("MCR", mcr | DUF)
            hz = max(self.mclk_input_hz() * ((new >> 12 & 0xFF) + 1) / ((new & 0xFFF) + 1), 1.0)
            self.chip.sched.cancel(self._duf_ev)
            self._duf_ev = self.chip.sched.at_ns(self.chip.now_ns() + 2e9 / hz, self._duf_clear)
            self._store(reg, new)
            self._mclk_update()
        return None

    def _duf_clear(self) -> None:
        self._duf_ev = None
        self.set("MCR", self.get("MCR") & ~DUF)
