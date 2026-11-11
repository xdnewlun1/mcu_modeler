"""STM32F4 I2S mode of SPI2/SPI3 (I2SCFGR.I2SMOD = 1) and the full-duplex extensions I2S2ext/I2S3ext.

References: RM0090 Rev 22 ch. 28.4 (I2S functional description), 28.5.8/28.5.9 (I2SCFGR, I2SPR) and 7.3.23
(RCC_PLLI2SCFGR), the register fields of STM32F405.svd (chips/stm32f405_regs.yaml) and ST's HAL
(stm32f4xx_hal_i2s.c / _i2s_ex.c / stm32f4xx_hal_rcc_ex.c, firmware/vendor/STM32CubeF4).  `I2SEngine` is
used by stm32.spi (chipem/periph/stm32/spi.py) for SPI2/SPI3; `I2SExt` (`stm32.i2s_ext`) is the I2SxEXT block.

Clock (28.4.4).  I2SxCLK = RCC `i2s_clk_hz()`: PLLI2S (PLL input / PLLM x PLLI2SN / PLLI2SR, needs PLLI2SON) or the
I2S_CKIN pin (CFGR.I2SSRC = 1, frequency from the RCC param `i2s_ckin_hz`; 0 = no clock: one `warn`, idle).
d = 2 x I2SDIV + ODD (28.5.9: I2SDIV 0 or 1 is forbidden: one `warn`, 2 is used).  28.4.4 formulas: MCKOE = 0:
CK = I2SxCLK / d, so Fs = I2SxCLK / (32 d) (16-bit channels) or / (64 d) (32-bit channels).  MCKOE = 1: MCK =
I2SxCLK / d = 256 Fs and CK = MCK / 8 (16-bit channels) or MCK / 4 (32-bit channels).  MCK is output while I2SMOD = 1,
master mode and MCKOE = 1 (28.4.1; that I2SE is not required, so a codec gets its master clock before the stream
starts, is this model's reading: RM0090 does not say); up to `params.mck_edges_max_hz`
(1 MHz) it toggles edge by edge, faster it stays low and is summarised as `pin` `wave` events (chipem/periph/sdbus.py
ClockWave).  Bit edges are scheduled in the wall-clock domain (sched.at_ns): PLLI2S does not follow HCLK.

Frame (28.4.3, Figures 264-280).  Channel length L = 16 (CHLEN = 0, DATLEN = 16 bit) or 32 (CHLEN = 1, or DATLEN
24/32 bit, which force 32).  Stereo standards: a frame is a left and a right channel slot (2 L bits); PCM: one slot (L
bits, mono).  Data are MSB first.  Philips: WS low = left, WS changes one CK before the MSB (Figure 264); MSB justified:
WS high = left, WS changes with the MSB, data left aligned (Figure 270); LSB justified: like MSB but data right
aligned in the slot (Figures 273-277); PCM: frame sync on WS, short (PCMSYNC = 0: WS high for one CK before the MSB)
or long (WS high for 13 CK from the MSB, Figures 279/280).  CKPOL = 0: CK idles low, data and WS change on falling
edges and are sampled on rising edges; CKPOL = 1 the opposite.  A master transmitter sends zeros until a half-word
has been written, then starts with it at the next left slot (28.4.5: "the first data written into the Tx buffer
correspond to the channel Left data"; no partial frame).  A master starts clocking at I2SE = 1 with two lead-in bit
clocks (no data) in which WS shows the previous channel (Philips: high, others low; PCM short: the second one carries
the frame sync pulse), so that a receiver sees a WS edge before the first frame (not documented by RM0090; chosen so
that codecs synchronise on frame 0; the idle levels are those 28.4.8 gives for re-enabling a slave).  Clearing I2SE
stops a master at the end of the current 16-bit half-word (I2SE reads 0 at once; this is what the receiver switch-off
sequences of 28.4.5 imply, e.g. "wait 17 I2S clock cycles" for 16-bit data in a 32-bit LSB-justified channel); a
slave stops at once.  TXE reads 0 while the I2S is disabled (28.4.7) and is set again by I2SE = 1 with an empty TX
buffer.
DR is 16 bits wide.  16-bit data: one DR access per channel (16-bit data in a 32-bit channel: followed by 16 zero bits,
or preceded by them in LSB justified).  24/32-bit data: two accesses per channel, D[31:16] then D[15:0] (24-bit
Philips / MSB / PCM: D[23:8] then D[7:0] in bits 15:8 of the second access; 24-bit LSB justified: D[23:16] in bits 7:0
of the first access, then D[15:0]).  The TX buffer is moved to the shift register when its 16 bits start (TXE = 1,
CHSIDE = channel of the next half-word to write); received half-words go to the RX buffer after their 16th bit
(RXNE = 1, CHSIDE = channel just received; RXNE or OVR still set -> OVR, the half-word is lost, and so is every
later one until OVR is cleared by a DR read then an SR read, 28.4.8).  Empty TX buffer: a slave transmitter sets UDR
(28.4.8: cleared by an SR read) and sends zeros; a master sends zeros (no flag; UDR is a slave-transmitter flag).
Slave: CK and WS come from the pins muxed to I2Sx_CK / I2Sx_WS; the slave synchronises on the first WS edge (the
data side starts with the next left slot) and checks WS at every bit: a WS level that does not match the expected
frame position sets FRE (bit 8, cleared by an SR read) and the slave stays desynchronised until I2SE is cleared and
set again (28.4.8 recovery procedure).  BSY (28.4.7): master transmitter: 1 while the clock runs; master receiver:
kept 0; slave (and I2SxEXT): 1 while a half-word is transferred, 0 for one CK cycle between half-words (from the
sampling edge of a half-word's last bit to the next sampling edge).
IRQ (SPIx line, shared with I2SxEXT through `_util.shared_irq`): (TXE & TXEIE) | (RXNE & RXNEIE) |
((OVR | UDR | FRE) & ERRIE).  DMA: SPIx_TX / SPIx_RX (I2Sx_EXT_TX / _RX for the extension) while TXE & TXDMAEN /
RXNE & RXDMAEN.
I2SxEXT (28.4.4 full duplex): a slave on the internal CK/WS of SPIx (whether SPIx is master or slave) with its own SD
pin (I2SxEXT_SD) and its own DR/SR/CR2/I2SCFGR; it uses SPIx's frame format (I2SCFGR of the extension only gives the
direction: slave TX or slave RX; master settings there are ignored with one `warn`).  It starts at the next left slot
after both I2SE bits are set.  No own clock gate (SPIx's).
Pins: master CK/WS outputs (CK at its idle level while running), SD output while transmitting, MCK output; slave CK/WS
inputs.  Every bit drives the pins with PinBoard.drive_signal (so a board net / the i2s_codec part sees the
waveform; the event log decimates such fast pins into `wave` summaries).  When none of CK, WS, SD, the extension's SD
is muxed to a pin, a master runs one event per channel slot with the same timing (no pin activity).
Observation: `tx_frames` / `rx_frames` (deques of (left, right) sample values: 16/24/32 bit as in DATLEN, PCM: (v,
None)) and an `i2s` event per `params.event_frames` (64) frames and direction: {block, dir, frames, words (left
samples, at most 64), bits, msg} like kinetis.sai.
Not modelled: the I2S clock jitter / ODD duty cycle of the "reshaping stage" (Figure 282; average frequency exact),
slave-mode start conditions beyond WS synchronisation (28.4.6: I2SE at least two PCLK before the first CK), SPI-mode
use of I2SxEXT (one `warn`).
"""
from __future__ import annotations

import re
from collections import deque
from typing import Any

from ..sdbus import ClockWave

# SR bits used in I2S mode
RXNE, TXE, CHSIDE, UDR, OVR, BSY, FRE = 1, 2, 4, 8, 0x40, 0x80, 0x100
I2SE, I2SMOD = 1 << 10, 1 << 11
MCKOE, ODD = 1 << 9, 1 << 8
STD_PHILIPS, STD_MSB, STD_LSB, STD_PCM = 0, 1, 2, 3
STD_NAMES = ("Philips", "MSB justified", "LSB justified", "PCM")
_MASK = {"D": 0xFFFF, "H8": 0xFF00, "L8": 0x00FF}


class Geo:
    """Frame geometry and data layout of one I2SCFGR setting."""
    __slots__ = ("std", "pcm_long", "datlen", "L", "nslots", "F", "layout", "dseq", "bits", "ckpol")

    def __init__(self, cfg: int):
        self.std = (cfg >> 4) & 3
        self.pcm_long = bool(cfg >> 7 & 1)
        self.ckpol = bool(cfg >> 3 & 1)
        dl = (cfg >> 1) & 3
        if dl == 3:
            dl = 0
        self.datlen = dl
        self.bits = (16, 24, 32)[dl]
        self.L = 32 if (dl or cfg & 1) else 16
        self.nslots = 1 if self.std == STD_PCM else 2
        self.F = self.L * self.nslots
        lsb = self.std == STD_LSB
        if self.L == 16:
            lay = ["D"]
        elif dl == 0:
            lay = ["Z", "D"] if lsb else ["D", "Z"]
        elif dl == 1:
            lay = ["L8", "D"] if lsb else ["D", "H8"]
        else:
            lay = ["D", "D"]
        self.layout = lay
        self.dseq = [(s, c) for s in range(self.nslots) for c, k in enumerate(lay) if k != "Z"]

    def ws(self, p: int) -> bool:
        """WS level (True = high) during bit p of the frame (p = -2, -1: the lead-in bits before the first frame)."""
        L, F = self.L, self.F
        if self.std == STD_PHILIPS:
            return ((p + 1) % F) // L == 1
        if self.std == STD_PCM:
            if self.pcm_long:
                return 0 <= p < 13
            return p == F - 1 or p == -1
        return 0 <= p < L                         # MSB / LSB justified: high = left

    def ws_idle(self) -> bool:
        return self.std == STD_PHILIPS

    def first_p(self) -> int:
        """A master starts two bit clocks before the first frame (lead-in, no data) so that WS shows the previous
        channel for one full clock and then its first edge: a receiver synchronises on the very first frame."""
        return -2

    def sync_pos(self, ws: bool) -> int | None:
        """Frame position of the bit at which WS changed to `ws` (None: not a synchronisation point)."""
        L, F = self.L, self.F
        if self.std == STD_PHILIPS:
            return (L - 1) if ws else F - 1
        if self.std == STD_PCM:
            if not ws:
                return None
            return 0 if self.pcm_long else F - 1
        return 0 if ws else L

    def value(self, acc: int) -> int:
        if self.datlen == 0:
            return acc & 0xFFFF
        if self.datlen == 1:
            return acc & 0xFFFFFF if self.std == STD_LSB else (acc >> 8) & 0xFFFFFF
        return acc & 0xFFFFFFFF


class I2SEngine:
    """I2S state of one block (SPIx in I2S mode, or an I2SxEXT).  The SPIx engine is also the clock side."""

    def __init__(self, blk: Any, idx: str, ext: bool = False):
        self.blk = blk
        self.lp_owner = blk                       # the PWR freezer holds our events with the block's
        self.ext = ext
        self.idx = idx
        p = blk.params
        # serial clock pin function: I2Sx_CK (DS8626, STM32F4) or I2Sx_SCK (`params.i2s_ck: SCK`, the STM32F2 data
        # sheets DS15818 / DS17050 spelling)
        ck = str(p.get("i2s_ck", "CK"))
        self.sig = {"CK": f"I2S{idx}_{ck}", "WS": f"I2S{idx}_WS", "MCK": f"I2S{idx}_MCK",
                    "SD": f"I2S{idx}ext_SD" if ext else f"I2S{idx}_SD"}
        self.event_frames = int(p.get("event_frames", 64))
        self.mck_edges_max_hz = float(p.get("mck_edges_max_hz", 1e6))
        self.tx_frames: deque = deque(maxlen=100_000)
        self.rx_frames: deque = deque(maxlen=100_000)
        self.partner: I2SEngine | None = None     # main <-> ext
        self._wave = ClockWave(blk, self.sig["MCK"])
        self.drv: dict[str, str] = {}
        self.mck_ev = None
        self.mck_hz = 0.0
        self.init()

    def init(self) -> None:
        self.running = False
        self.ev = None
        self.p = 0
        self.geo: Geo | None = None
        self.synced = False
        self.fre_lock = False          # slave: FRE seen, no resynchronisation until I2SE is toggled
        self.active = False            # data side started (at a left slot)
        self.cur = 0
        self.acc = [0, 0]
        self.vals: list = [None, None]
        self.block: list[int] = []
        self.frames = 0
        self.half = 0.0
        self.t_next = 0.0
        self.word_mode = False
        self.stop_pending = False
        self.ws_prev: bool | None = None
        self.ck_prev = None
        self.tx_dir = False

    # ------------------------------------------------------------------ helpers
    @property
    def chip(self) -> Any:
        return self.blk.chip

    def cfg(self) -> int:
        return self.blk.get("I2SCFGR")

    def mode(self) -> int:
        return (self.cfg() >> 8) & 3

    def is_master(self) -> bool:
        return not self.ext and self.mode() >= 2

    def is_tx(self) -> bool:
        return self.mode() in (0, 2)

    def i2s_on(self) -> bool:
        return bool(self.cfg() & I2SMOD)

    def enabled(self) -> bool:
        c = self.cfg()
        return bool(c & I2SMOD and c & I2SE)

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

    def _out(self, k: str, lvl: str) -> None:
        """Drive pin role k at lvl (starts driving it if released)."""
        cur = self.drv.get(k)
        if cur == lvl:
            return
        self.drv[k] = lvl
        chip = self.chip
        pins = getattr(chip, "pins", None)
        if pins is None:
            return
        sig = self.sig[k]
        if cur is None:
            from ._util import gpio_resync
            gpio_resync(chip, sig)
        if pins.signal_pins(sig):
            pins.drive_signal(sig, lvl)

    def _release(self, k: str) -> None:
        if self.drv.pop(k, None) is None:
            return
        chip = self.chip
        pins = getattr(chip, "pins", None)
        if pins is None:
            return
        pins.release_signal(self.sig[k])
        from ._util import gpio_resync
        gpio_resync(chip, self.sig[k])

    def release_all(self) -> None:
        for k in list(self.drv):
            self._release(k)

    def pin_state(self, role: str) -> tuple[str, str | None]:
        lvl = self.drv.get(role)
        return ("out", lvl) if lvl is not None else ("in", None)

    def _sr(self, set_bits: int = 0, clr_bits: int = 0) -> None:
        self.blk._sr_mod(set_bits, clr_bits)

    # ------------------------------------------------------------------ clocks (main only)
    def _rcc(self) -> Any:
        return getattr(self.blk, "_rcc", None)

    def divider(self) -> int:
        pr = self.blk.get("I2SPR")
        div, odd = pr & 0xFF, bool(pr & ODD)
        if div < 2:
            self.blk._warn_once("i2sdiv", f"{self.blk.name}: I2SPR.I2SDIV = {div} is forbidden (RM0090 28.5.9): "
                                f"2 used")
            div, odd = 2, odd
        return 2 * div + (1 if odd else 0)

    def i2sclk_hz(self) -> float:
        r = self._rcc()
        g = getattr(r, "i2s_kernel_hz", None)        # per-APB I2S clock mux (STM32F446 I2S1SRC/I2S2SRC: stm32.rcc_f446)
        if g is not None:
            return float(g(self.blk.name))
        if (self.blk.params or {}).get("i2s_clock") == "sysclk":
            # STM32F1 high/XL density (RM0008 25.4.3): I2SxCLK is the system clock (SYSCLK = HCLK x AHB prescaler)
            s = getattr(r, "sysclk_hz", None)
            if s is not None:
                return float(s() if callable(s) else s)
            chip = self.blk.chip
            hpre = getattr(r, "hpre", 1)              # stm32f1.rcc: AHB prescaler (property)
            hpre = hpre() if callable(hpre) else hpre
            return float(chip.clock.hz) * float(hpre or 1) if chip is not None else 0.0
        return float(r.i2s_clk_hz()) if r is not None and hasattr(r, "i2s_clk_hz") else 0.0

    def mck_out_hz(self) -> float:
        return self.i2sclk_hz() / self.divider()

    def bitclk_hz(self, geo: Geo | None = None) -> float:
        geo = geo or Geo(self.cfg())
        clk = self.i2sclk_hz()
        if clk <= 0:
            return 0.0
        d = self.divider()
        if self.blk.get("I2SPR") & MCKOE:
            return clk / d / (8 if geo.L == 16 else 4)
        return clk / d

    def fs_hz(self) -> float:
        geo = Geo(self.cfg())
        return self.bitclk_hz(geo) / geo.F

    def clock_changed(self) -> None:
        self.mck_update()
        if self.running and self.is_master():
            hz = self.bitclk_hz(self.geo)
            if hz <= 0:
                self.blk._warn_once("noclk", f"{self.blk.name}: I2SxCLK stopped while the I2S master runs: stopped")
                self.stop()
            else:
                self.half = 0.5e9 / hz
        elif self.enabled() and not self.running and self.is_master():
            self.start()

    def mck_update(self) -> None:
        chip = self.chip
        if chip is None or self.ext:
            return
        want = self.i2s_on() and self.mode() >= 2 and bool(self.blk.get("I2SPR") & MCKOE)
        hz = self.mck_out_hz() if want else 0.0
        if hz == self.mck_hz and (want == ("MCK" in self.drv)):
            return
        chip.sched.cancel(self.mck_ev)
        self.mck_ev = None
        self.mck_hz = hz
        if not want:
            self._wave.set(0.0, False)
            self._release("MCK")
            return
        self._out("MCK", "L")
        if hz <= 0:
            self._wave.set(0.0, False)
            self.blk._warn_once("nomck", f"{self.blk.name}: MCKOE = 1 but I2SxCLK has no source (PLLI2S off / no "
                                f"I2S_CKIN): MCK stays low")
        elif hz > self.mck_edges_max_hz:
            self._wave.set(hz, False)
        else:
            self._wave.set(0.0, False)
            half = 0.5e9 / hz
            self.mck_ev = chip.sched.at_ns(chip.now_ns() + half, self._mck_tick, chip.now_ns() + half, half)

    def _mck_tick(self, t: float, half: float) -> None:
        self._out("MCK", "L" if self.drv.get("MCK") == "H" else "H")
        self.mck_ev = self.chip.sched.at_ns(t + half, self._mck_tick, t + half, half)

    # ------------------------------------------------------------------ start / stop (clock side)
    def sides(self) -> list["I2SEngine"]:
        out = [self]
        o = self.partner
        if o is not None and o.enabled() and o.running:
            out.append(o)
        return out

    def start(self) -> None:
        """I2SE set (main block)."""
        chip = self.chip
        if chip is None or self.running:
            return
        self.geo = geo = Geo(self.cfg())
        self.active = False
        self.acc = [0, 0]
        self.vals = [None, None]
        self.tx_dir = self.is_tx()
        o = self.partner
        if o is not None and o.enabled():
            o.arm()
        if self.is_master():
            hz = self.bitclk_hz(geo)
            if hz <= 0:
                self.blk._warn_once("noclk", f"{self.blk.name}: I2S master enabled without I2SxCLK (PLLI2S off or "
                                    f"I2S_CKIN without frequency): no clock")
                return
            self.running = True
            self.synced = True
            self.half = 0.5e9 / hz
            sigs = [self.sig["CK"], self.sig["WS"], self.sig["SD"]]
            if o is not None:
                sigs.append(o.sig["SD"])
            self.word_mode = not any(self._muxed(s) for s in sigs)
            idle = "H" if geo.ckpol else "L"
            self._out("WS", "H" if geo.ws_idle() else "L")    # WS before CK: no receiver sees a WS change at the
            self._out("CK", idle)                              # first (idle) CK level
            self.p = geo.first_p()
            if self.tx_dir:
                self._sr(BSY)                      # master TX: BSY high during all transfers; master RX: kept low
            self._drive_bit()                      # (RM0090 28.4.7)
            self.t_next = chip.now_ns()
            if self.word_mode:
                self._schedule_word()
            else:
                self.t_next += self.half
                self.ev = chip.sched.at_ns(self.t_next, self._tick, True)
        else:
            self.running = True
            self.synced = False
            self.fre_lock = False
            self.ws_prev = None
            self.ck_prev = self._level(self.sig["CK"])
        if self.tx_dir:
            self._out("SD", "L")

    def arm(self) -> None:
        """Extension: I2SE set (it runs on the main block's clock)."""
        if self.running:
            return
        self.geo = None
        self.running = True
        self.active = False
        self.tx_dir = self.is_tx()
        if self.mode() >= 2:
            self.blk._warn_once("extmaster", f"{self.blk.name}: I2SCFG master mode on an I2S extension: it is "
                                f"always a slave of SPI{self.idx} (slave TX/RX used)")
        if self.tx_dir:
            self._out("SD", "L")

    def stop(self) -> None:
        chip = self.chip
        if chip is not None:
            chip.sched.cancel(self.ev)
        self.ev = None
        was = self.running
        self.running = False
        self.synced = False
        self.active = False
        self.stop_pending = False
        for k in ("CK", "WS", "SD"):
            self._release(k)
        if was and chip is not None:
            self._sr(clr_bits=BSY)
        o = self.partner
        if not self.ext and o is not None and o.running:
            o.active = False

    def disable(self) -> None:
        """I2SE cleared."""
        if not self.running:
            return
        if self.ext:
            self.running = False
            self.active = False
            self._release("SD")
            self._sr(clr_bits=BSY)
            return
        if self.is_master() and not self.word_mode:
            self.stop_pending = True               # finish the current 16-bit half-word (28.4.5)
        else:
            self.stop()

    # ------------------------------------------------------------------ master clock
    def _drive_bit(self) -> None:
        geo = self.geo
        if not self.word_mode:
            self._out("WS", "H" if geo.ws(self.p) else "L")
        for sd in self.sides():
            sd.side_drive(self.p, geo)

    def _sample_bit(self) -> None:
        geo = self.geo
        p = self.p
        for sd in self.sides():
            sd.side_sample(p, geo)
            if p >= 0 and (sd.ext or not sd.is_master()):
                # slave BSY (RM0090 28.4.7): set while a half-word is transferred, low for one CK cycle between two
                # half-words (from the sampling edge of a half-word's last bit to the next sampling edge)
                if p % 16 == 15:
                    sd._sr(clr_bits=BSY)
                elif not sd.blk.get("SR") & BSY:
                    sd._sr(BSY)

    def _advance(self) -> bool:
        """Next bit; False when a pending stop ends the stream here (end of the current 16-bit half-word: RM0090
        28.4.5, the receiver switch-off sequences)."""
        geo = self.geo
        self.p = self.p + 1 if self.p < 0 else (self.p + 1) % geo.F
        if self.stop_pending and self.p % 16 == 0:
            self.stop()
            return False
        return True

    def _tick(self, sample: bool) -> None:
        self.ev = None
        if not self.running:
            return
        geo = self.geo
        idle = "H" if geo.ckpol else "L"
        if sample:
            self._out("CK", "L" if idle == "H" else "H")
            self._sample_bit()
        else:
            if not self._advance():
                return                             # stopped at the slot boundary: pins released
            self._drive_bit()                      # WS / SD change with the falling (drive) edge, before CK moves:
            self._out("CK", idle)                  # a receiver clocked by this edge already sees the new WS
        if self.running:
            self.t_next += self.half
            self.ev = self.chip.sched.at_ns(self.t_next, self._tick, not sample)

    def _schedule_word(self) -> None:
        geo = self.geo
        n = geo.L - (self.p % geo.L) if self.p >= 0 else 1
        self.t_next += n * 2 * self.half
        self.ev = self.chip.sched.at_ns(self.t_next, self._word, n)

    def _word(self, n: int) -> None:
        self.ev = None
        if not self.running:
            return
        for _ in range(n):
            self._sample_bit()
            if not self._advance():
                return
            self._drive_bit()
            if not self.running:
                return
        self._schedule_word()

    # ------------------------------------------------------------------ slave clock (pins)
    def pin_changed(self, pin: Any) -> None:
        if not self.running or self.ext or self.is_master():
            return
        board = self.chip.pins
        if not board.signal_matches(pin.owner, self.sig["CK"]):
            return
        lvl = pin.level()
        prev, self.ck_prev = self.ck_prev, lvl
        if lvl == prev or lvl not in ("H", "L") or prev not in ("H", "L"):
            return
        geo = self.geo
        idle = "H" if geo.ckpol else "L"
        if lvl != idle:
            self._slave_sample(self._level(self.sig["WS"]) == "H")
        elif self.synced:
            self.p = (self.p + 1) % geo.F
            for sd in self.sides():
                sd.side_drive(self.p, geo)

    def _slave_sample(self, ws: bool) -> None:
        geo = self.geo
        prev, self.ws_prev = self.ws_prev, ws
        if self.fre_lock:
            return
        if self.synced and geo.ws(self.p) != ws:
            # RM0090 28.4.8: FRE; the slave stays out of synchronisation until software disables and re-enables it
            self.synced = False
            self.fre_lock = True
            for sd in self.sides():
                sd.active = False
                sd._sr(clr_bits=BSY)
            self._sr(FRE)
            self.blk._warn_once("fre", f"{self.blk.name}: I2S slave frame error (WS changed at an unexpected bit): "
                                f"desynchronised until I2SE is cleared and set again")
            return
        if not self.synced:
            if prev is None or prev == ws:
                return
            pos = geo.sync_pos(ws)
            if pos is None:
                return
            self.p = pos
            self.synced = True
        self._sample_bit()

    # ------------------------------------------------------------------ data side (main and extension)
    def side_drive(self, p: int, geo: Geo) -> None:
        if not self.tx_dir:
            return
        if p < 0:
            self._out("SD", "L")
            return
        slot, b = divmod(p, geo.L)
        if b == 0:
            if slot == 0 and not self.active and (self.blk.txbuf is not None or not self.is_master()):
                self.active = True                 # a master starts with the first written half-word as left
            self.acc[slot] = 0
        if not self.active:
            self._out("SD", "L")
            return
        c, bc = divmod(b, 16)
        if bc == 0:
            kind = geo.layout[c]
            if kind == "Z":
                self.cur = 0
            else:
                blk = self.blk
                if blk.txbuf is None:
                    val = 0
                    if not self.is_master():
                        self._sr(UDR)
                else:
                    val = blk.txbuf
                    blk.txbuf = None
                    nxt = geo.dseq[(geo.dseq.index((slot, c)) + 1) % len(geo.dseq)][0]
                    self._sr(TXE | (CHSIDE if nxt else 0), 0 if nxt else CHSIDE)
                self.cur = val & _MASK[kind]
                self.acc[slot] = (self.acc[slot] << 16) | self.cur
        self._out("SD", "H" if self.cur >> (15 - bc) & 1 else "L")
        if b == geo.L - 1:
            self._slot_done(slot, geo, self.tx_frames, "tx")

    def side_sample(self, p: int, geo: Geo) -> None:
        if self.tx_dir or p < 0:
            return
        slot, b = divmod(p, geo.L)
        if b == 0:
            if slot == 0:
                self.active = True
            self.acc[slot] = 0
        if not self.active:
            return
        c, bc = divmod(b, 16)
        if bc == 0:
            self.cur = 0
        bit = 1 if self._level(self.sig["SD"]) == "H" else 0
        self.cur = (self.cur << 1) | bit
        if bc == 15:
            kind = geo.layout[c]
            if kind != "Z":
                val = self.cur & _MASK[kind]
                self.acc[slot] = (self.acc[slot] << 16) | val
                blk = self.blk
                if blk.get("SR") & (RXNE | OVR):
                    # RM0090 28.4.8: RX buffer not updated; while OVR is set every later half-word is lost too
                    if not blk.get("SR") & OVR:
                        blk._ovr_dr_read = False
                    self._sr(OVR)
                else:
                    blk.rxbuf = val
                    self._sr(RXNE | (CHSIDE if slot else 0), 0 if slot else CHSIDE)
        if b == geo.L - 1:
            self._slot_done(slot, geo, self.rx_frames, "rx")

    def _slot_done(self, slot: int, geo: Geo, store: deque, d: str) -> None:
        self.vals[slot] = geo.value(self.acc[slot])
        if slot != geo.nslots - 1:
            return
        fr = (self.vals[0], self.vals[1] if geo.nslots == 2 else None)
        self.vals = [None, None]
        if fr[0] is None:
            return
        store.append(fr)
        self.frames += 1
        if len(self.block) < 64:
            self.block.append(fr[0])
        if self.event_frames and self.frames % self.event_frames == 0:
            words, self.block = self.block, []
            head = " ".join(f"{w:X}" for w in words[:8])
            self.blk.emit("i2s", block=self.blk.name, dir=d, frames=self.event_frames, words=words, bits=geo.bits,
                          msg=f"{self.blk.name} {d}: {self.event_frames} frames ({STD_NAMES[geo.std]}), left "
                              f"{head}{' ...' if len(words) > 8 else ''}")


def block_index(name: str) -> str:
    m = re.search(r"(\d+)", name)
    return m.group(1) if m else ""
