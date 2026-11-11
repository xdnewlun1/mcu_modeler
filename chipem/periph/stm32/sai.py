"""STM32F42xxx/43xxx serial audio interface SAI1 (RM0090 Rev 22 ch. 29): two audio sub-blocks A and B, bit by bit on
the pins.

Registers from the SVD (chips/stm32f427_regs.yaml: ACR1/ACR2/AFRCR/ASLOTR/AIM/ASR/ACLRFR/ADR at +0x04..+0x20, the B
copies at +0x24..+0x40; GCR at +0x00 is not described by the SVD and is not modelled -- RM0090 29.17 lists no GCR);
behaviour hand-written.  One class `_Sub` per sub-block; `SAI` is the MMIO block (`stm32.sai`).

* Clock generator (29.9, Figure 289).  SAI_CK_x = RCC `sai_clk_hz(block)` (stm32.rcc_f42x: RCC_DCKCFGR.SAI1xSRC
  selects PLLSAI_Q / PLLSAIDIVQ, PLLI2S_Q / PLLI2SDIVQ or the I2S_CKIN pin, RM0090 6.3.25), or `params.ck_hz` when
  the RCC model has no such method.  Master, NODIV = 0: MCLK_x = SAI_CK_x / (2 x MCKDIV) (MCKDIV = 0: / 1) and
  SCK_x = MCLK_x x (FRL + 1) / 256, i.e. FS = MCLK / 256; NODIV = 1: SCK_x = SAI_CK_x, no MCLK.  MCLK is output on
  the MCLK_x pin while the master block is enabled (or OUTDRIV = 1) with NODIV = 0; up to `params.mck_edges_max_hz`
  (1 MHz) edge by edge, faster as `pin` `wave` summaries (chipem/periph/sdbus.py ClockWave).  29.13.5: a master
  enabled with NODIV = 0 and FRL + 1 not a power of two in 8..256 sets WCKCFG and is disabled at once (SAIxEN stays
  0).  A master enabled without SAI_CK_x logs one `warn` and stays silent.  Bit edges are scheduled in the wall-clock
  domain (sched.at_ns): the audio PLLs do not follow HCLK.
* Frame (29.7, 29.8).  F = FRL + 1 bit clocks.  FS is active (level FSPOL) for FSALL + 1 bit clocks starting at the
  first bit of slot 0 (FSOFF = 0) or one bit before it (FSOFF = 1).  NBSLOT + 1 slots of SLOTSZ bits (00: = data
  size, 01: 16, 10: 32); FSDEF = 0: slots back to back from bit 0; FSDEF = 1 (start of frame + channel side): the
  first half of the slots from bit 0, the second half from bit F/2 (Figure 285).  Data: DS bits (8/10/16/20/24/32)
  from bit FBOFF of a slot, MSB first (LSBFIRST = 0); a TX slot carries zeros before/after its data, a TX bit outside
  every slot (frame padding) is 0.  Inactive slots (SLOTEN = 0) move no FIFO data; with TRIS = 1 the SD output is
  released (high impedance) during inactive slots and the frame padding, otherwise it drives 0 (29.12.4; the RM's
  finer rule "released from the end of the last data bit when the next slot is inactive" is reduced to whole slots).
  CKSTR (29.17.1): 1 = outputs change on the falling SCK edge, inputs are sampled on the rising edge; 0 = outputs
  change on the rising edge, inputs are sampled on the falling edge (ST HAL sets CKSTR = 1 for transmitters, 0 for
  receivers in its I2S modes).  The SCK idles at the sampling-edge level (so the first edge is a drive edge).
* Master transmitter (29.4): enabling starts SCK at once; FS and the frame start only when the FIFO holds data (at a
  drive edge, after one lead-in bit with FS inactive so that receivers see an FS edge before frame 0; with FSOFF = 1
  a second lead-in bit carries the first FS assertion).  Then frames follow back to back; an empty
  FIFO at the start of an active slot sends 0 and sets OVRUDR (underrun, 29.13.1; Figure 295).  Master receiver:
  frames start at enable.
* Slave (29.4, 29.13.2/3): SCK and FS come from the SAI1_SCK_x / SAI1_FS_x pins (or, with SYNCEN = 01, from the other
  sub-block, whose SCK/FS/MCLK are then used internally and this block's pins are released, 29.5).  The block
  synchronises on the first FS active edge; afterwards an FS active edge at an unexpected bit sets AFSDET (no other
  effect) and a missing one at the expected bit sets LFSDET (the slave keeps counting).
* FIFO (29.10): 8 words of DS bits (right aligned) per sub-block; a DR write when full and a DR read when empty are
  ignored (reads 0).  SR.FLTH per Table: TX 0 empty / 1 <= 1/4 / 2 <= 1/2 / 3 <= 3/4 / 4 not full / 5 full;
  RX 0 empty / 1 < 1/4 / 2 < 1/2 / 3 < 3/4 / 4 not full / 5 full.  FREQ: TX FLTH <= FTH, RX FLTH > FTH (FTH 0..4;
  5..7 reserved: one `warn`, 4 used).  FFLUSH empties the FIFO (reads 0).  TX: one word popped at the start of each
  active slot (MONO with 2 slots: slot 1 repeats slot 0, one word per frame); RX: one word pushed after the last data
  bit of each active slot (MONO with 2 slots: slot 0 only); a full FIFO sets OVRUDR, loses the word and every later
  one until a frame reaches the overrun slot again with room in the FIFO (Figure 294).
* MUTE (29.12.1): TX: CR2.MUTE is strobed at each frame start; a muted frame sends 0 (or, with MUTEVAL = 1 and at
  most two slots, the last value of each slot) and still pops the FIFO.  RX: MUTEDET after MUTECNT consecutive frames
  whose active slots all received 0 (set once; re-armed by a non-zero slot or a disable).
* SAIxEN (29.15, 29.17.1): can be set only while it reads 0; clearing it stops the block at the end of the current
  frame (SAIxEN reads 1 until then; immediately when no frame runs).  Configuration writes while enabled are kept
  (geometry is latched at enable) with one `warn`.
* Flags / IRQ (29.14): SR = FLTH | LFSDET | AFSDET | CNRDY | FREQ | WCKCFG | MUTEDET | OVRUDR, cleared through CLRFR
  (write 1; reads 0).  SR resets to 0x08 (FREQ: transmitter, empty FIFO, 29.17.6).  The IRQ "SAI1" (87, looked up
  by name) is the OR over both sub-blocks of SR & IM.  DMA (29.16): request "SAI1_A" / "SAI1_B" (RM0090 Table 44)
  while FREQ & DMAEN.
* Pins: SAI1_SCK_x / SAI1_FS_x / SAI1_MCLK_x outputs of a master (inputs of an asynchronous slave), SAI1_SD_x output
  of a transmitter (input of a receiver); the GPIO model asks `signal_state()`.  Outputs are driven while SAIxEN = 1
  or OUTDRIV = 1.  When none of SCK, FS, SD (of the block and of a synchronous partner) is muxed to a pin, a master
  processes the bits of one slot per scheduler event with the same timing (no pin activity).
* Observation: `sub("A").tx_frames` / `rx_frames` (deques of tuples of the active slots' values per frame) and an
  `i2s` event per `params.event_frames` (64) frames and direction, as stm32.i2s / kinetis.sai do.
* RCC: APB2ENR.SAI1EN gate (access while gated: one `warn`), APB2RSTR.SAI1RST resets the block.  Stop mode
  (`clock_stop`): a running master's bit clock is frozen (the PLLs stop in Stop mode, RM0090 5.3.4) and restarts from
  the RCC clock-change notification.
Not modelled: AC'97 (PRTCFG = 10: one `warn`, treated as the free protocol; CNRDY never set), companding (COMP =
1x: one `warn`, data passed uncompanded), the GCR register / external synchronisation, PCLK >= 2 x SCK check (one
`warn` only), TRIS release inside an active slot's padding.
"""
from __future__ import annotations

from collections import deque
from typing import Any

from ..registry import register
from ..sdbus import ClockWave
from ._util import GatedBlock, build_regs, dma_request, gpio_resync, irq_number, rcc

# CR1
MODE_RX, MODE_SLAVE = 1, 2
PRTCFG_SH, DS_SH, LSBFIRST, CKSTR, SYNCEN_SH, MONO, OUTDRIV = 2, 5, 1 << 8, 1 << 9, 10, 1 << 12, 1 << 13
SAIEN, DMAEN, NODIV, MCKDIV_SH = 1 << 16, 1 << 17, 1 << 19, 20
# CR2
FTH_MASK, FFLUSH, TRIS, MUTE, MUTEVAL, MUTECNT_SH, COMP_SH = 7, 1 << 3, 1 << 4, 1 << 5, 1 << 6, 7, 14
# SR / IM / CLRFR bit positions
OVRUDR, MUTEDET, WCKCFG, FREQ, CNRDY, AFSDET, LFSDET = 1, 2, 4, 8, 0x10, 0x20, 0x40
STICKY = OVRUDR | MUTEDET | WCKCFG | CNRDY | AFSDET | LFSDET
DS_BITS = {2: 8, 3: 10, 4: 16, 5: 20, 6: 24, 7: 32}
DEPTH = 8
_CFG_REGS = ("FRCR", "SLOTR")


def tx_flth(n: int) -> int:
    return 0 if n == 0 else 5 if n >= DEPTH else 1 if n <= 2 else 2 if n <= 4 else 3 if n <= 6 else 4


def rx_flth(n: int) -> int:
    return 0 if n == 0 else 5 if n >= DEPTH else 1 if n < 2 else 2 if n < 4 else 3 if n < 6 else 4


class Geometry:
    """Frame layout latched at enable (29.7 / 29.8)."""
    __slots__ = ("F", "fsall", "fsoff", "fspol", "fsdef", "nslot", "slotsz", "ds", "fboff", "lsbfirst", "slots",
                 "slot_en", "bitmap", "starts", "ckstr", "mono", "tris")

    def __init__(self, cr1: int, cr2: int, frcr: int, slotr: int):
        self.F = (frcr & 0xFF) + 1
        self.fsall = ((frcr >> 8) & 0x7F) + 1
        self.fsdef = bool(frcr >> 16 & 1)
        self.fspol = bool(frcr >> 17 & 1)
        self.fsoff = bool(frcr >> 18 & 1)
        self.ds = DS_BITS.get((cr1 >> DS_SH) & 7, 8)
        sz = (slotr >> 6) & 3
        self.slotsz = {0: self.ds, 1: 16, 2: 32}.get(sz, 32)
        self.slotsz = max(self.slotsz, self.ds)
        self.nslot = ((slotr >> 8) & 0xF) + 1
        self.fboff = slotr & 0x1F
        self.slot_en = (slotr >> 16) & 0xFFFF
        self.lsbfirst = bool(cr1 & LSBFIRST)
        self.ckstr = bool(cr1 & CKSTR)
        self.mono = bool(cr1 & MONO) and self.nslot == 2
        self.tris = bool(cr2 & TRIS)
        F = self.F
        bm: list = [None] * F
        starts = []
        if self.fsdef:
            per = max(self.nslot // 2, 1)
            half = F // 2
            for i in range(self.nslot):
                starts.append((i // per) * half + (i % per) * self.slotsz)
        else:
            starts = [i * self.slotsz for i in range(self.nslot)]
        for i, s0 in enumerate(starts):
            for b in range(self.slotsz):
                p = s0 + b
                if 0 <= p < F and bm[p] is None:
                    bm[p] = (i, b)
        self.bitmap = bm
        self.starts = starts

    def fs_active(self, p: int) -> bool:
        q = (p + (1 if self.fsoff else 0)) % self.F
        return q < self.fsall

    def fs_level(self, p: int | None) -> str:
        act = p is not None and self.fs_active(p)
        return "H" if act == self.fspol else "L"

    def enabled(self, slot: int) -> bool:
        return bool(self.slot_en >> slot & 1)

    def data_bit(self, word: int, b: int) -> int | None:
        """Bit value at slot bit b (None outside the data field)."""
        k = b - self.fboff
        if not 0 <= k < self.ds:
            return None
        return (word >> (k if self.lsbfirst else self.ds - 1 - k)) & 1


class _Sub:
    """One audio sub-block (A or B)."""

    def __init__(self, sai: "SAI", x: str):
        self.sai = sai
        self.x = x
        o = 0 if x == "A" else 0x20
        self.r = {k: f"{x}{k}" for k in ("CR1", "CR2", "FRCR", "SLOTR", "IM", "SR", "CLRFR", "DR")}
        self.off_dr = 0x20 + o
        self.sig = {k: f"SAI1_{k}_{x}" for k in ("SCK", "FS", "SD", "MCLK")}
        self.dma_name = f"SAI1_{x}"
        self.tx_frames: deque = deque(maxlen=100_000)
        self.rx_frames: deque = deque(maxlen=100_000)
        self.drv: dict[str, str] = {}
        self._wave = ClockWave(sai, self.sig["MCLK"])
        self.mck_ev = None
        self.mck_hz = 0.0
        self.init()

    # ------------------------------------------------------------------ state
    def init(self) -> None:
        self.fifo: deque = deque()
        self.flags = 0
        self.running = False
        self.disabling = False
        self.geo: Geometry | None = None
        self.ev = None
        self.p: int | None = None          # bit position in the frame (None: no frame yet)
        self.inframe = False                # p belongs to a real frame (not the FSOFF lead-in)
        self.synced = False
        self.fs_prev: bool | None = None
        self.half = 0.0
        self.t_next = 0.0
        self.word_mode = False
        self.cur: dict[int, int] = {}       # slot -> word being shifted
        self.last: dict[int, int] = {}      # slot -> last value sent (MUTEVAL)
        self.frame_vals: list = []
        self.muted = False
        self.mute_cnt = 0
        self.ovr_slot: int | None = None
        self.frames = 0
        self.block: list[int] = []
        self.ck_prev: str | None = None
        self._dma_on = False
        self.underruns = 0
        self.overruns = 0

    def cr1(self) -> int:
        return self.sai.get(self.r["CR1"])

    def cr2(self) -> int:
        return self.sai.get(self.r["CR2"])

    def is_slave(self) -> bool:
        return bool(self.cr1() & MODE_SLAVE)

    def is_rx(self) -> bool:
        return bool(self.cr1() & MODE_RX)

    def sync(self) -> bool:
        return (self.cr1() >> SYNCEN_SH) & 3 == 1

    def other(self) -> "_Sub":
        return self.sai.subs["B" if self.x == "A" else "A"]

    # ------------------------------------------------------------------ status
    def fifo_level(self) -> int:
        n = len(self.fifo)
        return rx_flth(n) if self.is_rx() else tx_flth(n)

    def freq(self) -> bool:
        fth = self.cr2() & FTH_MASK
        if fth > 4:
            self.sai._warn_once(f"fth{self.x}", f"SAI1_{self.x}CR2.FTH = {fth} is reserved: FIFO full used")
            fth = 4
        lv = self.fifo_level()
        return lv > fth if self.is_rx() else lv <= fth

    def sr(self) -> int:
        return self.flags | (FREQ if self.freq() else 0) | (self.fifo_level() << 16)

    def update(self) -> None:
        """Re-evaluate the IRQ line and the DMA request after any state change."""
        sai = self.sai
        sai.update_irq()
        want = bool(self.cr1() & DMAEN) and self.freq()
        chip = sai.chip
        if chip is None:
            return
        if want:
            dma_request(chip, self.dma_name, True)       # (re-)arm: the DMA acknowledges one item per request
            self._dma_on = True
        elif self._dma_on:
            dma_request(chip, self.dma_name, False)
            self._dma_on = False

    def set_flag(self, f: int) -> None:
        if not self.flags & f:
            self.flags |= f
            self.update()

    # ------------------------------------------------------------------ FIFO (CPU / DMA side)
    def dr_write(self, v: int) -> None:
        if self.is_rx():
            return
        if len(self.fifo) >= DEPTH:
            return                                  # 29.17.8: loaded only if the FIFO is not full
        self.fifo.append(v & 0xFFFFFFFF)             # right aligned; the upper bits are dropped when sent
        self.update()

    def dr_read(self) -> int:
        if not self.is_rx() or not self.fifo:
            return 0
        v = self.fifo.popleft()
        self.update()
        return v

    def flush(self) -> None:
        if self.fifo:
            self.fifo.clear()
        self.update()

    # ------------------------------------------------------------------ pins
    def _muxed(self, sig: str) -> bool:
        pins = getattr(self.sai.chip, "pins", None)
        if pins is None:
            return False
        return any(pins.signal_matches(p.owner, sig) for p in pins.signal_pins(sig))

    def _level(self, sig: str) -> str | None:
        pins = getattr(self.sai.chip, "pins", None)
        if pins is None:
            return None
        for p in pins.signal_pins(sig):
            if pins.signal_matches(p.owner, sig):
                return p.level()
        return None

    def out(self, k: str, lvl: str) -> None:
        cur = self.drv.get(k)
        if cur == lvl:
            return
        self.drv[k] = lvl
        chip = self.sai.chip
        pins = getattr(chip, "pins", None)
        if pins is None:
            return
        sig = self.sig[k]
        if cur is None:
            gpio_resync(chip, sig)
        if pins.signal_pins(sig):
            pins.drive_signal(sig, lvl)

    def release(self, k: str) -> None:
        if self.drv.pop(k, None) is None:
            return
        chip = self.sai.chip
        pins = getattr(chip, "pins", None)
        if pins is None:
            return
        pins.release_signal(self.sig[k])
        gpio_resync(chip, self.sig[k])

    def release_all(self) -> None:
        for k in list(self.drv):
            self.release(k)

    def signal_state(self, role: str) -> tuple[str, str | None]:
        lvl = self.drv.get(role)
        return ("out", lvl) if lvl is not None else ("in", None)

    def outputs_on(self) -> bool:
        c = self.cr1()
        return bool(c & SAIEN or c & OUTDRIV or self.running)

    def idle_outputs(self) -> None:
        """Drive the idle levels of the outputs this configuration owns (SAIxEN or OUTDRIV, 29.17.1)."""
        c = self.cr1()
        g = self.geo or Geometry(c, self.cr2(), self.sai.get(self.r["FRCR"]), self.sai.get(self.r["SLOTR"]))
        master = not c & MODE_SLAVE
        if master and not self.sync():
            self.out("SCK", "H" if g.ckstr else "L")
            self.out("FS", g.fs_level(None))
        if not c & MODE_RX:
            self.out("SD", "L")

    # ------------------------------------------------------------------ clocks
    def ck_hz(self) -> float:
        return self.sai.sai_ck_hz(self.x)

    def mclk_hz(self) -> float:
        c = self.cr1()
        div = (c >> MCKDIV_SH) & 0xF
        return self.ck_hz() / (2 * div if div else 1)

    def sck_hz(self, g: Geometry | None = None) -> float:
        c = self.cr1()
        if c & NODIV:
            return self.ck_hz()
        g = g or self.geo
        F = g.F if g is not None else (self.sai.get(self.r["FRCR"]) & 0xFF) + 1
        return self.mclk_hz() * F / 256

    def fs_hz(self) -> float:
        g = self.geo or Geometry(self.cr1(), self.cr2(), self.sai.get(self.r["FRCR"]), self.sai.get(self.r["SLOTR"]))
        return self.sck_hz(g) / g.F

    def mck_update(self) -> None:
        chip = self.sai.chip
        if chip is None:
            return
        c = self.cr1()
        want = (not c & MODE_SLAVE) and not c & NODIV and not self.sync() and (self.running or bool(c & OUTDRIV))
        hz = self.mclk_hz() if want else 0.0
        if hz == self.mck_hz and want == ("MCLK" in self.drv):
            return
        chip.sched.cancel(self.mck_ev)
        self.mck_ev = None
        self.mck_hz = hz
        if not want:
            self._wave.set(0.0, False)
            self.release("MCLK")
            return
        self.out("MCLK", "L")
        mx = self.sai.mck_edges_max_hz
        if hz <= 0:
            self._wave.set(0.0, False)
        elif hz > mx:
            self._wave.set(hz, False)
        else:
            self._wave.set(0.0, False)
            half = 0.5e9 / hz
            t = chip.now_ns() + half
            self.mck_ev = chip.sched.at_ns(t, self._mck_tick, t, half)

    def _mck_tick(self, t: float, half: float) -> None:
        self.out("MCLK", "L" if self.drv.get("MCLK") == "H" else "H")
        self.mck_ev = self.sai.chip.sched.at_ns(t + half, self._mck_tick, t + half, half)

    def clock_changed(self) -> None:
        self.mck_update()
        if not self.running or self.is_slave() or self.sync():
            return
        hz = self.sck_hz()
        if hz <= 0:
            self.sai._warn_once(f"noclk{self.x}", f"SAI1 block {self.x}: SAI_CK_{self.x} stopped while the master "
                                f"runs: bit clock stopped")
            self.sai.chip.sched.cancel(self.ev)
            self.ev = None
            return
        self.half = 0.5e9 / hz
        if self.ev is None:
            self._schedule(self.sai.chip.now_ns())

    # ------------------------------------------------------------------ enable / disable
    def enable(self) -> bool:
        """SAIxEN 0 -> 1. False: refused (WCKCFG)."""
        chip = self.sai.chip
        c = self.cr1()
        g = Geometry(c, self.cr2(), self.sai.get(self.r["FRCR"]), self.sai.get(self.r["SLOTR"]))
        master = not c & MODE_SLAVE
        if master and not c & NODIV and g.F not in (8, 16, 32, 64, 128, 256):
            self.flags |= WCKCFG                     # 29.13.5: automatically disabled
            self.update()
            self.sai.emit("warn", block=self.sai.name, msg=f"SAI1 block {self.x}: FRL + 1 = {g.F} is not a power of "
                          f"two in 8..256 with NODIV = 0: WCKCFG set, block disabled (RM0090 29.13.5)")
            return False
        if (c >> PRTCFG_SH) & 3 == 2:
            self.sai._warn_once("ac97", "SAI1: AC'97 protocol is not modelled: free protocol used")
        if (self.cr2() >> COMP_SH) & 3 >= 2:
            self.sai._warn_once("comp", "SAI1: companding (u-law / A-law) is not modelled: data are not companded")
        if g.fboff + g.ds > g.slotsz or g.nslot * g.slotsz > g.F + (g.F if g.fsdef else 0):
            self.sai._warn_once(f"geo{self.x}", f"SAI1 block {self.x}: slot configuration violates RM0090 29.8 "
                                f"(FBOFF <= SLOTSZ - DS, NBSLOT x SLOTSZ <= FRL)")
        self.geo = g
        self.running = True
        self.disabling = False
        self.p = None
        self.inframe = False
        self.synced = False
        self.fs_prev = None
        self.cur = {}
        self.frame_vals = []
        self.mute_cnt = 0
        self.ovr_slot = None
        self.muted = False
        self.idle_outputs()
        if self.sync():
            if self.other().sync():
                self.sai._warn_once("bothsync", "SAI1: both sub-blocks have SYNCEN = 01: nothing drives the clock")
            for k in ("SCK", "FS", "MCLK"):
                self.release(k)
        elif master:
            hz = self.sck_hz(g)
            if hz <= 0:
                self.sai._warn_once(f"noclk{self.x}", f"SAI1 block {self.x}: master enabled without SAI_CK_{self.x} "
                                    f"(PLLSAI / PLLI2S off or no I2S_CKIN): no bit clock")
            else:
                self.half = 0.5e9 / hz
                pclk = self.sai.pclk_hz()
                if pclk and pclk < 2 * hz:
                    self.sai._warn_once("pclk", f"SAI1: PCLK2 {pclk / 1e6:g} MHz is below twice the bit clock "
                                        f"{hz / 1e6:g} MHz (RM0090 29.5 note)")
                sigs = [self.sig["SCK"], self.sig["FS"], self.sig["SD"]]
                o = self.other()
                if o.running and o.sync():
                    sigs.append(o.sig["SD"])
                self.word_mode = not any(self._muxed(s) for s in sigs)
                if not c & MODE_RX:
                    pass                                # master TX: frames start once the FIFO has data
                else:
                    self._start_frame()
                self._schedule(chip.now_ns())
        else:
            self.ck_prev = self._level(self.sig["SCK"])
        self.mck_update()
        self.update()
        return True

    def _start_frame(self) -> None:
        """Master: start the frame sequence with lead-in bits at the end of a virtual previous frame: one bit with FS
        inactive, then (FSOFF = 1) the bit that carries the first FS assertion (29.7.4), so that a receiver sees an
        FS edge before frame 0 (not documented by RM0090; the same choice as stm32.i2s)."""
        g = self.geo
        self.p, self.inframe = (g.F - 3 if g.fsoff else g.F - 2), False

    def request_disable(self) -> None:
        if not self.running:
            return
        if self.sync() and not self.other().running:
            self.stop()                               # no clock any more: nothing to complete
        elif self.inframe or (self.p is not None and not self.is_slave()):
            self.disabling = True                     # 29.15: the current frame is completed
        else:
            self.stop()

    def stop(self) -> None:
        chip = self.sai.chip
        if chip is not None:
            chip.sched.cancel(self.ev)
        self.ev = None
        self.running = False
        self.disabling = False
        self.inframe = False
        self.p = None
        self.synced = False
        sai = self.sai
        sai.set(self.r["CR1"], self.cr1() & ~SAIEN)
        if not self.cr1() & OUTDRIV:
            for k in ("SCK", "FS", "SD"):
                self.release(k)
        self.mck_update()
        self.update()
        o = self.other()
        if o.running and o.sync() and not self.sync() and o.disabling:
            o.stop()                                   # its frame ended with the clock it was waiting for

    # ------------------------------------------------------------------ master clock
    def followers(self) -> list["_Sub"]:
        o = self.other()
        return [self, o] if (o.running and o.sync() and not self.sync()) else [self]

    def _schedule(self, t0: float) -> None:
        chip = self.sai.chip
        if self.word_mode:
            n = self._bits_to_boundary()
            self.t_next = t0 + n * 2 * self.half
            self.ev = chip.sched.at_ns(self.t_next, self._word, n)
        else:
            self.t_next = t0 + self.half
            self.ev = chip.sched.at_ns(self.t_next, self._tick, True)

    def _bits_to_boundary(self) -> int:
        g = self.geo
        p = self.p
        if p is None:
            return 1
        for s0 in sorted(set(g.starts + [g.F])):
            if s0 > p:
                return s0 - p
        return 1

    def _tick(self, drive: bool) -> None:
        self.ev = None
        if not self.running:
            return
        g = self.geo
        if drive:
            self._master_drive()                       # FS / SD change with the drive edge, before SCK moves: a
            if self.running:                           # receiver clocked by this edge already sees the new FS
                self.out("SCK", "L" if g.ckstr else "H")
        else:
            self.out("SCK", "H" if g.ckstr else "L")
            fs = self.drv.get("FS") == ("H" if g.fspol else "L")
            for b in self.followers():
                b.on_sample(fs)
        if self.running:
            self.t_next += self.half
            self.ev = self.sai.chip.sched.at_ns(self.t_next, self._tick, not drive)

    def _word(self, n: int) -> None:
        self.ev = None
        if not self.running:
            return
        g = self.geo
        for _ in range(n):
            self._master_drive()
            if not self.running:
                return
            fs = g.fs_active(self.p) if (self.p is not None and (self.inframe or g.fsoff)) else False
            for b in self.followers():
                b.on_sample(fs)
        self._schedule(self.t_next)

    def _master_drive(self) -> None:
        """Drive edge of a master: advance the frame position, then FS and the data of every block on this clock."""
        g = self.geo
        if self.p is None:                             # master TX waiting for data (29.4)
            if self.fifo:
                self._start_frame()
            else:
                return
        p = self.p + 1
        if p >= g.F:
            p = 0
        if p == 0:
            if self.disabling and self.inframe:
                self.stop()
                return
            self.inframe = True
        self.p = p
        self.out("FS", g.fs_level(p))
        for b in self.followers():
            b.on_drive()

    # ------------------------------------------------------------------ slave clock (pins)
    def pin_changed(self, pin: Any) -> None:
        if not self.running or not self.is_slave() or self.sync():
            return
        board = self.sai.chip.pins
        if not board.signal_matches(pin.owner, self.sig["SCK"]):
            return
        lvl = pin.level()
        prev, self.ck_prev = self.ck_prev, lvl
        if lvl == prev or lvl not in ("H", "L") or prev not in ("H", "L"):
            return
        g = self.geo
        sample_lvl = "H" if g.ckstr else "L"         # CKSTR = 1: sampled on the rising edge
        bl = [self] + ([self.other()] if self.other().running and self.other().sync() else [])
        if lvl == sample_lvl:
            fs = self._level(self.sig["FS"]) == ("H" if g.fspol else "L")
            for b in bl:
                b.on_sample(fs)
        else:
            for b in bl:
                b.on_drive()

    # ------------------------------------------------------------------ data side (every block on a clock)
    def on_drive(self) -> None:
        """Drive edge of the clock this block runs on (its own master clock, the SCK pin, or the synchronous
        partner's clock): advance the frame position (slaves) and drive SD (transmitters)."""
        if not self.running:
            return
        g = self.geo
        if self.is_slave() or self.sync():
            if not self.synced:
                if not self.is_rx():
                    self._drive_sd(None)
                return
            p = self.p + 1
            if p >= g.F:
                p -= g.F
            if p == 0 and self.disabling:
                self.stop()
                return
            self.p = p
        else:
            p = self.p                                # the master advanced it in _master_drive
            if p is None:
                return
        if p == 0:
            self._frame_start()
        if not self.is_rx():
            self._drive_sd(p if self.inframe else None)

    def on_sample(self, fs_active: bool) -> None:
        if not self.running:
            return
        g = self.geo
        slave_like = self.is_slave() or self.sync()
        if slave_like:
            prev, self.fs_prev = self.fs_prev, fs_active
            edge = fs_active and prev is False
            start = g.F - 1 if g.fsoff else 0
            if not self.synced:
                if edge:
                    self.synced = True
                    self.p = start
                    self.inframe = not g.fsoff
                    if not g.fsoff:
                        self._frame_start()
                else:
                    return
            elif edge and self.p != start:
                if not self.flags & AFSDET:
                    self.set_flag(AFSDET)             # 29.13.2: no effect on the current frame
            elif self.p == start and not edge and not self.flags & LFSDET:
                self.set_flag(LFSDET)                 # 29.13.3
        if self.is_rx() and self.inframe and self.p is not None:
            self._sample_sd(self.p)
        if self.p == g.F - 1 and self.inframe:
            self._frame_end()

    # ------------------------------------------------------------------ frame and slot handling
    def _frame_start(self) -> None:
        self.inframe = True
        self.frame_vals = []
        self.muted = bool(self.cr2() & MUTE) and not self.is_rx()      # strobed at the frame boundary (29.12.1)

    def _frame_end(self) -> None:
        g = self.geo
        vals = tuple(self.frame_vals)
        self.frame_vals = []
        if self.is_rx() and vals:
            if all(v == 0 for v in vals):
                self.mute_cnt += 1
                cnt = (self.cr2() >> MUTECNT_SH) & 0x3F
                if self.mute_cnt == cnt:
                    self.set_flag(MUTEDET)
            else:
                self.mute_cnt = 0
        store = self.rx_frames if self.is_rx() else self.tx_frames
        if vals:
            store.append(vals)
            self.frames += 1
            if len(self.block) < 64:
                self.block.append(vals[0])
            ef = self.sai.event_frames
            if ef and self.frames % ef == 0:
                words, self.block = self.block, []
                d = "rx" if self.is_rx() else "tx"
                head = " ".join(f"{w:X}" for w in words[:8])
                self.sai.emit("i2s", block=f"{self.sai.name}_{self.x}", dir=d, frames=ef, words=words, bits=g.ds,
                              msg=f"{self.sai.name} block {self.x} {d}: {ef} frames, slot 0 {head}"
                                  f"{' ...' if len(words) > 8 else ''}")
        if self.p is not None and not self.is_slave() and not self.sync() and self.disabling:
            pass                                      # master: stopped at the next drive edge (p wraps to 0)

    def _load_slot(self, slot: int) -> int:
        """TX: the word sent in an active slot (pops the FIFO; underrun -> 0)."""
        g = self.geo
        if g.mono and slot == 1:
            v = self.cur.get(0, 0)
        elif self.fifo:
            v = self.fifo.popleft() & ((1 << g.ds) - 1)
            self.update()
        else:
            v = 0
            self.underruns += 1
            self.set_flag(OVRUDR)                     # 29.13.1 underrun (also a slave transmitter)
            return self._mute_value(slot, 0, underrun=True)
        return self._mute_value(slot, v)

    def _mute_value(self, slot: int, v: int, underrun: bool = False) -> int:
        g = self.geo
        if self.muted:
            if self.cr2() & MUTEVAL and g.nslot <= 2:
                return self.last.get(slot, 0)
            return 0
        if not underrun:
            self.last[slot] = v
        return v

    def _drive_sd(self, p: int | None) -> None:
        g = self.geo
        bm = g.bitmap[p] if p is not None else None
        if bm is None:
            if p is not None and g.tris:
                self.release("SD")
            else:
                self.out("SD", "L")
            return
        slot, b = bm
        if not g.enabled(slot):
            if g.tris:
                self.release("SD")
            else:
                self.out("SD", "L")
            return
        if b == 0:
            v = self._load_slot(slot)
            self.cur[slot] = v
            self.frame_vals.append(v)
        bit = g.data_bit(self.cur.get(slot, 0), b)
        self.out("SD", "H" if bit else "L")

    def _sample_sd(self, p: int) -> None:
        g = self.geo
        bm = g.bitmap[p]
        if bm is None:
            return
        slot, b = bm
        if not g.enabled(slot):
            return
        if b == 0:
            self.cur[slot] = 0
        k = b - g.fboff
        if not 0 <= k < g.ds:
            return
        bit = 1 if self._level(self.sig["SD"]) == "H" else 0
        if g.lsbfirst:
            self.cur[slot] |= bit << k
        else:
            self.cur[slot] = (self.cur[slot] << 1) | bit
        if k == g.ds - 1:
            v = self.cur[slot]
            self.frame_vals.append(v)
            if g.mono and slot == 1:
                return                                # 29.12.2: slot 1 discarded
            if self.ovr_slot is not None:
                if slot != self.ovr_slot or len(self.fifo) >= DEPTH:
                    return                            # Figure 294: resume at the overrun slot
                self.ovr_slot = None
            if len(self.fifo) >= DEPTH:
                self.overruns += 1
                self.ovr_slot = slot
                self.set_flag(OVRUDR)
                return
            self.fifo.append(v)
            self.update()


@register("stm32.sai")
class SAI(GatedBlock):
    model = "stm32.sai"

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.gate = params.get("gate", "SAI1")
        ov: dict[str, dict] = {}
        for x in "AB":
            ov[f"{x}CR1"] = {"on_write": self._cr1_write(x), "on_read": self._cr1_read(x)}
            ov[f"{x}CR2"] = {"on_write": self._cr2_write(x)}
            ov[f"{x}FRCR"] = {"on_write": self._cfg_write(x)}
            ov[f"{x}SLOTR"] = {"on_write": self._cfg_write(x), "ro_mask": 0x0000F020}
            ov[f"{x}IM"] = {"on_write": self._im_write(x), "ro_mask": 0xFFFFFF80}
            ov[f"{x}SR"] = {"reset": FREQ, "ro_mask": 0xFFFFFFFF, "on_read": self._sr_read(x)}
            ov[f"{x}CLRFR"] = {"on_write": self._clrfr_write(x), "on_read": lambda b, r: 0}
            ov[f"{x}DR"] = {"on_write": self._dr_write(x), "on_read": self._dr_read(x)}
        r = build_regs((name, "SAI1", "SAI"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.event_frames = int(params.get("event_frames", 64))
        self.mck_edges_max_hz = float(params.get("mck_edges_max_hz", 1e6))
        self.ck_hz_param = float(params.get("ck_hz", 0.0))
        self.subs = {x: _Sub(self, x) for x in "AB"}
        self._irqn: int | None = None
        self._line = False
        self._warned: set[str] = set()
        self._listen: set[str] = set()

    # ------------------------------------------------------------------ setup
    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irqn = irq_number(chip, self.name, "SAI1")
        r = rcc(chip)
        if r is not None:
            r.listeners.append(self._clock_changed)
        board = getattr(chip, "pins", None)
        if board is not None:
            for s in self.subs.values():
                for p in board.signal_pins(s.sig["SCK"]):
                    if p.id not in self._listen:
                        self._listen.add(p.id)
                        board.input_listeners[p.id].append(self._pin_changed)

    def reset(self) -> None:
        chip = self.chip
        for s in getattr(self, "subs", {}).values():
            if chip is not None:
                chip.sched.cancel(s.ev)
                chip.sched.cancel(s.mck_ev)
                if s._dma_on:
                    dma_request(chip, s.dma_name, False)
            s.mck_ev = None
            s.mck_hz = 0.0
            s._wave.reset()
            s.release_all()
            s.init()
        if getattr(self, "_line", False) and chip is not None and self._irqn is not None:
            chip.arch.set_irq(self._irqn, False)
        self._line = False
        if hasattr(self, "_warned"):
            self._warned.clear()
        super().reset()

    def clock_stop(self, stopped: bool, mode: str) -> None:
        """Stop mode: the audio PLLs stop (RM0090 5.3.4); a running master's bit clock freezes."""
        if self.chip is None:
            return
        for s in self.subs.values():
            if stopped:
                self.chip.sched.cancel(s.ev)
                s.ev = None
            elif s.running:
                s.clock_changed()

    # ------------------------------------------------------------------ bus access (clock gate check)
    def read(self, offset: int, size: int) -> int:
        self._gate_ok()
        return super().read(offset, size)

    def write(self, offset: int, size: int, value: int) -> None:
        self._gate_ok()
        super().write(offset, size, value)

    # ------------------------------------------------------------------ helpers
    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=msg)

    def sub(self, x: str) -> _Sub:
        return self.subs[x.upper()]

    def sai_ck_hz(self, x: str) -> float:
        r = rcc(self.chip) if self.chip is not None else None
        f = getattr(r, "sai_clk_hz", None)
        if f is not None:
            return float(f(x))
        return self.ck_hz_param

    def pclk_hz(self) -> float:
        r = rcc(self.chip) if self.chip is not None else None
        try:
            return float(r.effective_apb_hz(2)) if r is not None else 0.0
        except Exception:
            return 0.0

    def update_irq(self) -> None:
        if self._irqn is None or self.chip is None:
            return
        on = any(s.sr() & self.get(s.r["IM"]) & 0x7F for s in self.subs.values())
        if on != self._line:
            self._line = on
            self.chip.arch.set_irq(self._irqn, on)

    def _clock_changed(self) -> None:
        for s in self.subs.values():
            s.clock_changed()

    def _pin_changed(self, pin: Any) -> None:
        for s in self.subs.values():
            s.pin_changed(pin)

    def signal_state(self, sig: str) -> tuple[str, str | None] | None:
        for s in self.subs.values():
            for k, v in s.sig.items():
                if v == sig:
                    return s.signal_state(k)
        return None

    # ------------------------------------------------------------------ register hooks
    def _cr1_read(self, x: str):
        def rd(blk, reg):
            s = self.subs[x]
            v = self.get(reg.name) & ~SAIEN
            return v | (SAIEN if s.running else 0)
        return rd

    def _cr1_write(self, x: str):
        def wr(blk, reg, old, new):
            s = self.subs[x]
            was = s.running
            if was and (old ^ new) & 0x00F80FEF:      # MCKDIV NODIV SYNCEN CKSTR LSBFIRST DS PRTCFG MODE
                self._warn_once(f"cr1{x}", f"SAI_{x}CR1 configuration changed while SAI{x}EN = 1 (RM0090 29.17.1: "
                                f"configure with the block disabled): applied at the next enable")
            if new & SAIEN and not was:
                s.enable()                            # WCKCFG: refused, SAIxEN stays 0 (29.13.5)
            elif not new & SAIEN and was:
                s.request_disable()                   # reads 1 until the end of the frame (29.15)
            # SAIxEN set while still reading 1 (disabling): ignored, "can be set only if it is at 0" (29.17.1)
            cur = (new & ~SAIEN) | (SAIEN if s.running else 0)
            self._store(reg, cur)
            if not s.running:
                if cur & OUTDRIV:
                    s.idle_outputs()
                elif old & OUTDRIV:
                    for k in ("SCK", "FS", "SD"):
                        s.release(k)
            s.mck_update()
            s.update()
            return cur
        return wr

    def _cr2_write(self, x: str):
        def wr(blk, reg, old, new):
            s = self.subs[x]
            if new & FFLUSH:
                if s.running:
                    self._warn_once(f"flush{x}", f"SAI_{x}CR2.FFLUSH while the block is enabled: FIFO data lost")
                s.flush()
            s.update()
            return new & ~FFLUSH
        return wr

    def _cfg_write(self, x: str):
        def wr(blk, reg, old, new):
            if self.subs[x].running and old != new:
                self._warn_once(f"cfg{x}{reg.name}", f"SAI_{reg.name} changed while SAI{x}EN = 1 (RM0090 29.17: "
                                f"configure with the block disabled): applied at the next enable")
            return None
        return wr

    def _im_write(self, x: str):
        def wr(blk, reg, old, new):
            self.update_irq()
            return None
        return wr

    def _sr_read(self, x: str):
        return lambda blk, reg: self.subs[x].sr()

    def _clrfr_write(self, x: str):
        def wr(blk, reg, old, new):
            s = self.subs[x]
            s.flags &= ~(new & STICKY)
            s.update()
            return 0
        return wr

    def _dr_write(self, x: str):
        def wr(blk, reg, old, new):
            self.subs[x].dr_write(new)
            return 0
        return wr

    def _dr_read(self, x: str):
        return lambda blk, reg: self.subs[x].dr_read()
