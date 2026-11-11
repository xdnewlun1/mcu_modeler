"""STM32F446 SPDIF receiver (SPDIFRX, RM0390 Rev 9 ch. 27).

Input: an S/PDIF stream from a board part on the pin selected by CR.INSEL (INSEL n = the pin function SPDIFRX_IN<n>;
RM0390 numbers the inputs SPDIFRX_IN1..4 for INSEL 0..3, ST's pin data / the chip YAML SPDIFRX_IN0..3).  The line is
summarised, not decoded edge by edge: a transmitter part (chipem/parts/spdif_source.py) hands the receiver blocks of
IEC 60958 sub-frames with their timing through `spdif_subframes()`; the model processes every sub-frame at the
instant its last bit cell ends (one scheduler event per sub-frame while a stream is decoded).

What follows RM0390 (section numbers):
* 27.3.4 / 27.3.5 states: SPDIFRXEN 00 STATE_IDLE, 01 STATE_SYNC, 11 STATE_RCV (via SYNC), STATE_STOP after FERR /
  SERR / TERR.  IDLE resets the decoder, flushes RX_BUF and clears FERR / SERR / TERR (27.3.5, 27.5.3).  RCV -> SYNC
  is refused (`warn`); 10 is reserved (field write ignored, `warn`).
* Synchronisation (Figure 345 / 346): WFA = 1 waits for activity on the input; then the coarse synchronisation (70
  transitions, ~0.73 frame) and a preamble search, then 40 symbols decoded -> SYNCD is set 40 symbol times after the
  first sub-frame that starts 0.75 frame after the synchronisation began.  No stream (or WFA = 0 with nothing on the
  line): TRCNT overflows after 8192 SPDIFRX_CLK periods -> TERR (27.3.8; also when a stream stops later).
  SPDIFRX_CLK below 11 x the symbol rate (704 x Fs, 27.3.3 / 27.3.9 / Table 171): the thresholds cannot be computed,
  every attempt fails, SERR after NBTR + 1 attempts (1, 4, 16, 64; one attempt per frame) and one `warn`.  PCLK1 below
  the symbol rate (27.3.9): one `warn`.  No SPDIFRX_CLK at all (PLL_R / PLLI2S_P off): the decoder does not run.
* After SYNCD: SR.WIDTH5 = 5 symbols in SPDIFRX_CLK periods, DIR.TLO / THI = 1.5 / 2.5 UI in SPDIFRX_CLK periods
  (27.5.3, 27.5.9; symbol = 1 / (64 Fs), UI = symbol / 2).  SBD on every "B" preamble (27.5.3).  Control flow
  (27.3.7): from the first "B" preamble after SYNCD, every 8 frames CSR = SOB | CS[7:0] (channel A, or B with CHSEL)
  | USR[15:0] (A, B, A, B ...; USR[0] and CS[0] the oldest) and CSRNE (cleared by reading CSR; DMA request
  SPDIFRX_CS while CSRNE and CBDMAEN).  Once the block structure is known a preamble that is not where expected is an
  FERR (27.3.8).
* Data flow (27.3.6): in STATE_RCV samples are stored from the next "B" preamble on; RX_BUF + DR double buffer; a word
  goes to DR when DR is empty, else to RX_BUF, else it is dropped with OVR (27.3.8).  RXSTEO = 1 keeps A/B alignment:
  a sample whose channel equals the last stored one is dropped (Figure 351); RXSTEO = 0 stores the next sample as soon
  as there is room (Figure 350).  DRFMT 00 / 01 / 10 per 27.5.5-27.5.7 with PMSK / VMSK / CUMSK / PTMSK (applied when
  the word is built; PE is the sub-frame's own parity error); DRFMT = 10 packs two sub-frames (bits 23:8; the older one
  in [31:16]).  RXNE cleared by the DR read (which moves RX_BUF into DR); DMA request SPDIFRX_DT while RXNE and
  RXDMAEN.  PERR on an odd parity sub-frame (27.3.8), reception continues.
* FERR: a sub-frame without a valid preamble (biphase coding error), only when synchronised.
* Register protection Table 172: INSEL / WFA / NBTR / CHSEL writable only in IDLE, DRFMT / RXSTEO not in RCV (except in
  the write that enters RCV); IFCR (SYNCDCF, SBDCF, OVRCF, PERRCF) is write-only; reserved bits read 0.
* Interrupt SPDIF_RX = (SR & IMR) with IFEIE covering SERR / TERR / FERR (Figure 352).

Approximations: the synchronisation time is a fixed function of the stream (no jitter, no retries with a good clock);
the frame-sync output (spdifrx_frame_sync to the timers) is not modelled; the input glitch filter and the threshold
tracking are implicit (WIDTH5 / TLO / THI are the ideal values for the current SPDIFRX_CLK); preamble types are taken
from the part, not decoded from the biphase pattern.
"""
from __future__ import annotations

from collections import deque
from typing import Any

from ..base import pure_read
from ..registry import register
from ._util import GatedBlock, apb_hz, build_regs, dma_request, irq_number, rcc

# CR
EN_MASK, RXDMAEN, RXSTEO, PMSK, VMSK, CUMSK, PTMSK, CBDMAEN, CHSEL, WFA = 3, 4, 8, 0x40, 0x80, 0x100, 0x200, 0x400, \
    0x800, 0x4000
DRFMT_MASK = 0x30
CR_VALID = 0x00077FFF
IDLE_ONLY = 0x00070000 | WFA | 0x3000 | CHSEL         # INSEL, WFA, NBTR, CHSEL (Table 172)
RCV_RO = DRFMT_MASK | RXSTEO
# SR / IMR
RXNE, CSRNE, PERR, OVR, SBD, SYNCD, FERR, SERR, TERR = 1, 2, 4, 8, 0x10, 0x20, 0x40, 0x80, 0x100
IFE_FLAGS = FERR | SERR | TERR
PT = {"B": 1, "M": 2, "W": 3}
ATTEMPTS = (1, 4, 16, 64)


@register("stm32.spdifrx")
class SPDIFRX(GatedBlock):
    model = "stm32.spdifrx"

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.gate = params.get("gate", "SPDIFRX")
        ov = {"CR": {"ro_mask": ~CR_VALID & 0xFFFFFFFF, "on_write": self._cr_write},
              "IMR": {"ro_mask": 0xFFFFFF80, "on_write": lambda b, r, o, n: self._update()},
              "SR": {"ro_mask": 0xFFFFFFFF},
              "IFCR": {"on_read": pure_read(lambda b, r: 0), "on_write": self._ifcr_write},
              "DR": {"ro_mask": 0xFFFFFFFF, "on_read": self._dr_read, "on_peek": lambda b, r: None},
              "CSR": {"ro_mask": 0xFFFFFFFF, "on_read": self._csr_read, "on_peek": lambda b, r: None},
              "DIR": {"ro_mask": 0xFFFFFFFF}}
        r = build_regs((name, "SPDIF_RX", "SPDIFRX"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self._irqn: int | None = None
        self._warned: set[str] = set()
        self.samples: deque = deque(maxlen=100_000)      # observation: every word stored into DR / RX_BUF
        self._init_state()

    # ------------------------------------------------------------------ state
    def _init_state(self) -> None:
        self.rx_state = "IDLE"
        self._q: deque = deque()               # pending sub-frames: (t_end_ns, index, subframe)
        self._ev = None
        self._terr_ev = None
        self._sync_ev = None
        self._line = False
        self._dma_dt = self._dma_cs = False
        self._rxbuf: int | None = None
        self._synced = False
        self._sync_t: float | None = None      # when the synchronisation (re)started
        self._active = False                   # a stream was seen since enable (WFA)
        self._rate = 0.0
        self._cu_on = False                    # control flow acquisition started (first B after SYNCD)
        self._cu: list[tuple[int, int, int]] = []
        self._cs_bits: list[int] = []
        self._usr_bits: list[int] = []
        self._sob = False
        self._rcv_on = False                   # RCV: samples stored (first B after entering RCV)
        self._last_ch: int | None = None
        self._pack: int | None = None          # DRFMT 10: first half pending
        self._frame_pos: int | None = None     # frame index within the 192-frame block (after the first B)
        self._n = 0

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irqn = irq_number(chip, "SPDIF_RX", "SPDIFRX")

    def reset(self) -> None:
        self._cancel_all()
        if self.chip is not None:
            if self._line and self._irqn is not None:
                self.irq(self._irqn, False)
            if self._dma_dt:
                dma_request(self.chip, "SPDIFRX_DT", False)
            if self._dma_cs:
                dma_request(self.chip, "SPDIFRX_CS", False)
        self._init_state()
        self._warned.clear()
        super().reset()

    def _cancel_all(self) -> None:
        if self.chip is not None:
            for ev in (self._ev, self._terr_ev, self._sync_ev):
                if ev is not None:
                    self.chip.sched.cancel(ev)
        self._ev = self._terr_ev = self._sync_ev = None

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=f"{self.name}: {msg}")

    def _now(self) -> float:
        return float(self.chip.now_ns())

    def _clk(self) -> float:
        r = rcc(self.chip) if self.chip is not None else None
        f = getattr(r, "spdifrx_clk_hz", None) if r is not None else None
        try:
            return float(f()) if f is not None else 0.0
        except Exception:  # noqa: BLE001
            return 0.0

    # ------------------------------------------------------------------ flags / IRQ / DMA
    def _sr(self) -> int:
        return self.get("SR")

    def _set_sr(self, v: int) -> None:
        self.set("SR", v)
        self._update()

    def _flag(self, f: int) -> None:
        self._set_sr(self._sr() | f)

    def _update(self) -> None:
        if self.chip is None:
            return None
        sr, imr, cr = self._sr(), self.get("IMR"), self.get("CR")
        en = sr & imr & (RXNE | CSRNE | PERR | OVR | SBD | SYNCD)
        if imr & 0x40 and sr & IFE_FLAGS:
            en |= 1
        line = bool(en)
        if line != self._line and self._irqn is not None:
            self._line = line
            self.irq(self._irqn, line)
        dt = bool(sr & RXNE and cr & RXDMAEN)
        if dt != self._dma_dt:
            self._dma_dt = dt
            dma_request(self.chip, "SPDIFRX_DT", dt)
        cs = bool(sr & CSRNE and cr & CBDMAEN)
        if cs != self._dma_cs:
            self._dma_cs = cs
            dma_request(self.chip, "SPDIFRX_CS", cs)
        return None

    # ------------------------------------------------------------------ registers
    def _cr_write(self, blk, reg, old, new):
        oen, nen = old & EN_MASK, new & EN_MASK
        if nen == 2:
            self._warn_once("en10", "SPDIFRXEN = 10 is reserved (RM0390 27.5.1): field write ignored")
            nen = oen
        if oen == 3 and nen == 1:
            self._warn_once("rcv2sync", "SPDIFRXEN 11 -> 01 is not allowed (RM0390 27.5.1: go through 00)")
            nen = 3
        if oen != 0:
            new = (new & ~IDLE_ONLY) | (old & IDLE_ONLY)          # Table 172: rw only in STATE_IDLE
        if oen == 3:
            new = (new & ~RCV_RO) | (old & RCV_RO)                # read-only in STATE_RCV
        new = (new & ~EN_MASK) | nen
        self.set("CR", new)
        if oen == 0 and nen:
            self._start_sync()
        elif oen and not nen:
            self._to_idle()
        elif nen == 3 and oen == 1:
            self._enter_rcv()
        self._update()
        return new

    def _ifcr_write(self, blk, reg, old, new):
        clr = new & (SYNCD | SBD | OVR | PERR)
        if clr:
            self._set_sr(self._sr() & ~clr)
        return 0

    def _dr_read(self, blk, reg):
        if self.rx_state == "IDLE" and not self._sr() & RXNE:
            return None
        self._advance(self._now())
        v = self.get("DR")
        sr = self._sr()
        if sr & RXNE:
            if self._rxbuf is not None:
                self.set("DR", self._rxbuf)
                self._rxbuf = None
            else:
                sr &= ~RXNE
            self._set_sr(sr)
        return v

    def _csr_read(self, blk, reg):
        self._advance(self._now())
        v = self.get("CSR")
        if self._sr() & CSRNE:
            self._set_sr(self._sr() & ~CSRNE)
        return v

    # ------------------------------------------------------------------ state machine
    def _start_sync(self) -> None:
        clk = self._clk()
        self.rx_state = "SYNC"
        self._synced = False
        self._cu_on = self._rcv_on = False
        self._frame_pos = None
        if clk <= 0:
            self._warn_once("noclk", "no SPDIFRX_CLK (RCC DCKCFGR2.SPDIFSEL: PLL_R or PLLI2S_P not running): the "
                            "decoder does not run")
            return
        self._sync_t = self._now()
        self._active = False
        if not self.get("CR") & WFA:
            self._arm_terr(self._sync_t)

    def _enter_rcv(self) -> None:
        self._rcv_on = False          # samples from the next B preamble on (27.3.5)
        self._last_ch = None
        self._pack = None

    def _to_idle(self) -> None:
        self._cancel_all()
        self.rx_state = "IDLE"
        self._q.clear()
        self._rxbuf = None
        self._synced = False
        self._pack = None
        self._set_sr(self._sr() & ~(FERR | SERR | TERR | RXNE | CSRNE))

    def _stop(self, flag: int) -> None:
        self._cancel_all()
        self._q.clear()
        self.rx_state = "STOP"
        self._flag(flag)

    def _terr_ns(self) -> float:
        clk = self._clk()
        return 8192 / clk * 1e9 if clk > 0 else 0.0

    def _arm_terr(self, t_last: float) -> None:
        if self.chip is None:
            return
        if self._terr_ev is not None:
            self.chip.sched.cancel(self._terr_ev)
        self._terr_ev = self.chip.sched.at_ns(max(t_last + self._terr_ns(), self._now()), self._terr)

    def _terr(self) -> None:
        self._terr_ev = None
        if self.rx_state in ("SYNC", "RCV"):
            self._advance(self._now())
            if self.rx_state in ("SYNC", "RCV"):
                self._stop(TERR)                 # TRCNT overflow: no transition for 8192 SPDIFRX_CLK periods

    # ------------------------------------------------------------------ input hook (spdif_source part)
    def _insel_sig(self) -> str:
        return f"SPDIFRX_IN{(self.get('CR') >> 16) & 7}"

    def spdif_accepts(self, pin: Any) -> bool:
        board = getattr(self.chip, "pins", None)
        if board is None:
            return False
        return board.signal_matches(pin.owner, self._insel_sig()) and pin.direction != "analog"

    def spdif_subframes(self, source: Any, t0: float, sub_ns: float, subs: list) -> None:
        if self.rx_state not in ("SYNC", "RCV") or self._sync_t is None:
            return
        self._rate = 1e9 / (2 * sub_ns)
        clk = self._clk()
        for k, sf in enumerate(subs):
            self._q.append((t0 + (k + 1) * sub_ns, t0 + k * sub_ns, sf))
        self._arm_terr(t0 + len(subs) * sub_ns)
        if not self._active:
            self._active = True
            start = max(self._sync_t, t0)
            self._sync_t = start
            frame = 2 * sub_ns
            if clk < 704 * self._rate:
                self._warn_once("clk", f"SPDIFRX_CLK {clk / 1e6:.3f} MHz < 704 x Fs = {704 * self._rate / 1e6:.3f} MHz "
                                "(RM0390 27.3.9 Table 171): synchronisation fails")
                n = ATTEMPTS[(self.get("CR") >> 12) & 3]
                self._sync_at(start + n * frame, fail=True)
            else:
                if apb_hz(self.chip, 1) < 64 * self._rate:
                    self._warn_once("pclk", "PCLK1 below the S/PDIF symbol rate (RM0390 27.3.9)")
                # first sub-frame starting 0.75 frame after the start, then 40 symbols (40/64 frame)
                tb = start + 0.75 * frame
                k0 = max(0, -(-(tb - t0) // sub_ns))
                self._sync_at(t0 + k0 * sub_ns + 40 / 64 * frame)
        self._arm()

    def _sync_at(self, t: float, fail: bool = False) -> None:
        if self._sync_ev is not None:
            self.chip.sched.cancel(self._sync_ev)
        self._sync_ev = self.chip.sched.at_ns(max(t, self._now()), self._sync_done, fail)

    def _sync_done(self, fail: bool) -> None:
        self._sync_ev = None
        if self.rx_state not in ("SYNC", "RCV"):
            return
        self._advance(self._now())
        if fail:
            self._stop(SERR)
            return
        self._synced = True
        clk = self._clk()
        ui = 1.0 / (128 * self._rate)
        width5 = min(0x7FFF, round(5 * 2 * ui * clk))
        tlo, thi = min(0x1FFF, round(1.5 * ui * clk)), min(0x1FFF, round(2.5 * ui * clk))
        self.set("DIR", (tlo << 16) | thi)
        self._set_sr((self._sr() & 0xFFFF) | (width5 << 16) | SYNCD)
        if self.get("CR") & EN_MASK == 3:
            self.rx_state = "RCV"
            self._enter_rcv()
        # sub-frames that ended before SYNCD are not decoded
        t = self._now()
        while self._q and self._q[0][0] <= t:
            self._q.popleft()
        self._arm()

    # ------------------------------------------------------------------ sub-frame processing
    def _arm(self) -> None:
        if self.chip is None:
            return
        if self._ev is not None:
            self.chip.sched.cancel(self._ev)
            self._ev = None
        if self._synced and self._q and self.rx_state in ("SYNC", "RCV"):
            self._ev = self.chip.sched.at_ns(max(self._q[0][0], self._now()), self._tick)

    def _tick(self) -> None:
        self._ev = None
        self._advance(self._now())
        self._arm()

    def _advance(self, t: float) -> None:
        if not self._synced:
            return
        while self._q and self._q[0][0] <= t + 1e-6 and self.rx_state in ("SYNC", "RCV"):
            _te, _ts, sf = self._q.popleft()
            self._subframe(sf)

    def _subframe(self, sf: tuple) -> None:
        pre, data, v, u, c, perr = sf
        if self.get("CR") & EN_MASK == 3 and self.rx_state == "SYNC":
            self.rx_state = "RCV"
            self._enter_rcv()
        if pre is None:
            self._stop(FERR)                     # biphase coding error / preamble missing (27.3.8)
            return
        ch = 1 if pre == "W" else 0
        if pre == "B":
            if self._frame_pos is not None and self._frame_pos != 0:
                self._stop(FERR)                 # preamble at an unexpected place
                return
            self._frame_pos = 0
            self._flag(SBD)
            if not self._cu_on:
                self._cu_on = True
                self._sob = True
                self._cs_bits, self._usr_bits = [], []
            if self.rx_state == "RCV" and not self._rcv_on:
                self._rcv_on = True
        elif pre == "M" and self._frame_pos == 0 and ch == 0:
            self._stop(FERR)                     # B expected every 192 frames
            return
        if perr:
            self._flag(PERR)
        # control flow (27.3.7)
        if self._cu_on:
            self._usr_bits.append(u)
            chsel = 1 if self.get("CR") & CHSEL else 0
            if ch == chsel:
                self._cs_bits.append(c)
            if len(self._usr_bits) == 16 and len(self._cs_bits) == 8:
                cs = sum(b << i for i, b in enumerate(self._cs_bits))
                usr = sum(b << i for i, b in enumerate(self._usr_bits))
                self.set("CSR", (int(self._sob) << 24) | (cs << 16) | usr)
                self._sob = False
                self._cs_bits, self._usr_bits = [], []
                self._flag(CSRNE)
        # data flow (27.3.6)
        if self.rx_state == "RCV" and self._rcv_on:
            self._store(sf, ch)
        if ch == 1 and self._frame_pos is not None:
            self._frame_pos = (self._frame_pos + 1) % 192

    def _word(self, sf: tuple) -> int:
        pre, data, v, u, c, perr = sf
        cr = self.get("CR")
        pe = 0 if cr & PMSK else int(bool(perr))
        vv = 0 if cr & VMSK else v
        uu, cc = (0, 0) if cr & CUMSK else (u, c)
        pt = 0 if cr & PTMSK else PT.get(pre, 0)
        st = pe | vv << 1 | uu << 2 | cc << 3 | pt << 4
        if (cr & DRFMT_MASK) >> 4 == 1:
            return ((data & 0xFFFFFF) << 8) | st
        return (data & 0xFFFFFF) | st << 24

    def _store(self, sf: tuple, ch: int) -> None:
        cr = self.get("CR")
        fmt = (cr & DRFMT_MASK) >> 4
        stereo = bool(cr & RXSTEO)
        if fmt == 2:
            half = (sf[1] >> 8) & 0xFFFF
            if self._pack is None or (stereo and ch == 0):
                if stereo and ch == 1:
                    return                       # RXSTEO = 1: ChA always in [31:16] (27.3.6)
                self._pack = half
                return
            word = (self._pack << 16) | half
            self._pack = None
        else:
            if stereo and self._last_ch is not None and ch == self._last_ch:
                return                           # keep A/B alignment after an overrun (Figure 351)
            word = self._word(sf)
        sr = self._sr()
        if not sr & RXNE:
            self.set("DR", word)
            self._flag(RXNE)
        elif self._rxbuf is None:
            self._rxbuf = word
        else:
            self._flag(OVR)                      # both DR and RX_BUF full: the sample is lost (27.3.8)
            return
        self._last_ch = ch
        self.samples.append(word)

    # ------------------------------------------------------------------ introspection
    def state_info(self) -> dict:
        return {"state": self.rx_state, "synced": self._synced, "rate": self._rate, "pending": len(self._q)}
