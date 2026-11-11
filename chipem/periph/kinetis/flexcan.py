"""Kinetis FlexCAN (CAN0 on the K22F): 16 message buffers, Rx FIFO, masks, error confinement, on a chipem.canbus bus.

Sources: K22 Sub-Family Reference Manual (K22P100M120SF5V2RM, Rev. 5) ch. 46 "CAN (FlexCAN)": §46.3.2 MCR, §46.3.3
CTRL1, §46.3.4 TIMER, §46.3.5-7 RXMGMASK/RX14MASK/RX15MASK, §46.3.8 ECR (fault confinement rules), §46.3.9 ESR1,
§46.3.10-11 IMASK1/IFLAG1, §46.3.12 CTRL2, §46.3.13 ESR2, §46.3.14 CRCR, §46.3.15-17 RXFGMASK/RXFIR/RXIMR, §46.3.34
message buffer structure (Tables 46-70/46-71 CODE state machines), §46.3.35 Rx FIFO structure, §46.4.1-46.4.7
(transmit, arbitration, receive, matching Table 46-77/46-78, move-in, abort, inactivation, lock mechanism, Rx FIFO),
§46.4.8.1 remote frames, §46.4.8.4 bit timing, §46.4.10 freeze / disable modes; ch. 3 §3.9.2 (16 MBs, MDIS set
after reset, CLKSRC = bus clock or OSCERCLK, SIM_SCGC6[FLEXCAN0] gate, interrupt sources ORed onto CAN0_ORed_Message
_buffer / CAN0_Bus_Off / CAN0_Error / CAN0_Tx_Warning / CAN0_Rx_Warning / CAN0_Wake_Up).  Register table: SVD
(chips/mk22_regs.yaml CAN0).  The bus (timing, arbitration, ACK) is chipem/canbus.py.

Modes: MCR.MDIS = 1 (reset) disabled: LPMACK = NOTRDY = 1.  MDIS = 0 with FRZ & HALT (reset values) = freeze mode:
FRZACK = NOTRDY = 1, no bus activity, configuration writable.  Leaving freeze (HALT or FRZ cleared): FRZACK and NOTRDY
clear at once, the module then synchronises to the bus (11 recessive bit times) before it transmits or receives.
Freeze is granted at once (a frame of ours already on the wire still completes).  "Freeze-only" fields (MCR RFEN,
SUPV, WRNEN, SRXDIS, IRMQ, LPRIOEN, AEN, IDAM, MAXMB; CTRL1 bit timing, LPB, LOM, SMP, TSYN, LBUF; CTRL2; the masks;
ECR) keep their old value when written outside freeze/disable; CTRL1.CLKSRC only in disable mode (MDIS = 1).
MCR.SOFTRST resets MCR (except MDIS), TIMER, ECR, ESR1, ESR2, IMASK1, IFLAG1, CRCR and the internal state; it
self-clears at once.

Bit time: CAN engine clock = bus clock (CTRL1.CLKSRC = 1) or OSCERCLK (CLKSRC = 0: the MCG model's external
oscillator `ext_hz`, 0 while OSC0_CR.ERCLKEN = 0); Sclock = clock / (PRESDIV + 1); bit = (1 + (PROPSEG + 1) +
(PSEG1 + 1) + (PSEG2 + 1)) time quanta.  TIMER is the 16-bit free-running bit counter (runs outside freeze/disable);
MB time stamps take its value at the second identifier bit (SOF + 2 bit times).

Transmit (Table 46-71): CODE 0xC (DATA, or REMOTE with RTR) / 0xE (TANSWER) MBs between the first non-FIFO MB and
MAXMB compete; the internal winner is the lowest MB with CTRL1.LBUF, else the lowest arbitration value (frame ID
bits, PRIO in front with MCR.LPRIOEN), ties to the lower MB.  After transmission: time stamp, CODE -> INACTIVE (0x8),
EMPTY (0x4, remote request: the MB becomes an Rx MB) or RANSWER (0xA, after TANSWER), CRCR = (MB, CRC), IFLAG.
Abort (MCR.AEN): writing ABORT (0x9) to a Tx MB that is on the bus is blocked and kept pending: the frame completes
(CODE INACTIVE, IFLAG) or fails (CODE ABORT, IFLAG); otherwise the MB is inactivated at once with CODE ABORT and IFLAG
set.  Without AEN a CODE write simply (re)programs the MB (a frame already on the wire is sent without notification).
Self reception (MCR.SRXDIS = 0, always in loop-back): our own acknowledged frame is matched against our Rx MBs.

Receive (Tables 46-70, 46-77, 46-78): a frame (acknowledged on the bus) is matched against active Rx MBs (EMPTY 0x4,
FULL 0x2, OVERRUN 0x6) from the lowest number: ID compared under the mask (MCR.IRMQ = 1: RXIMRn; else RXMGMASK,
RX14MASK/RX15MASK for MB14/15; a 1 bit is "compare"), IDE always compared and RTR never (CTRL2.EACEN = 0) or both
under mask bits 30/31 (EACEN = 1).  IRMQ = 0: the first matching MB wins; IRMQ = 1: the first free-to-receive one
(EMPTY, or FULL/OVERRUN already serviced), else the last matching one (overrun).  Move-in: data, ID, SRR/IDE/RTR/DLC,
time stamp; CODE EMPTY -> FULL, FULL/OVERRUN -> FULL if serviced else OVERRUN; IFLAG.  Lock: reading the C/S word of a
FULL/OVERRUN MB (or any MB with IRMQ = 0) locks it until TIMER or another MB's C/S word is read (or C/S is written);
a move-in to a locked MB waits (the newest pending frame wins) and happens at unlock.  CTRL1.TSYN resets TIMER on a
reception into the first non-FIFO MB.  Remote frames: CTRL2.RRS = 0: matched (exact ID) against RANSWER MBs, which
become TANSWER and transmit their response; RRS = 1: stored like data frames.
Rx FIFO (MCR.RFEN): 6 frames deep, output at MB0 (0x80-0x8C, read as an MB) with RXFIR.IDHIT; ID filter table of
8 x (RFFN + 1) elements from 0xE0 (formats A/B/C, IDAM = 3 rejects everything), masked by RXIMRn for the first
8 + 2 x RFFN elements with IRMQ = 1, by RXFGMASK otherwise; occupies MB0 .. 7 + 2 x RFFN.  IFLAG BUF5I = frames
available (writing 1 pops the output, re-set while more remain), BUF6I = 5th frame stored (warning), BUF7I = frame
lost because the FIFO was full.  CTRL2.MRP selects FIFO-first (0) or MBs-first (1) matching.

Errors (§46.3.8, chipem.canbus.CanErrorCounters): ECR TXERRCNT/RXERRCNT; ESR1 FLTCONF (LOM reads error passive),
TXWRN/RXWRN, ACKERR (no other node acknowledged), BIT0ERR (TX/RX pin not muxed: the SOF reads back recessive;
listen-only node seeing an unacknowledged frame, REC unchanged), FRMERR (bit-rate mismatch on reception),
ERRINT, TWRNINT/RWRNINT (with MCR.WRNEN, counter reaching 96), BOFFINT (TEC > 255).  Reading ESR1 clears bits 15-10.
Bus off: no bus activity; recovery after 128 x 11 bit times (CTRL1.BOFFREC = 0) or, with BOFFREC = 1, once BOFFREC is
cleared (at the earliest 128 x 11 bit times after entering bus off, else 11 bit times later).  ESR1 SYNCH, IDLE, TX,
RX are computed from the bus.

Pins / listening: the node is on the bus only when SIM_SCGC6[FLEXCAN0] is on, MDIS = 0, out of freeze, synchronised,
not bus off, and a pin is muxed to CAN0_RX (a pin muxed to CAN0_TX as well to transmit; else bit errors, see
canbus).  A chip description with no pin offering the signal skips that check.  CTRL1.LPB: internal loop-back on a
private bus that acknowledges itself (the pins and the board bus are not involved).  CTRL1.LOM: receives, never
acknowledges or transmits, counters frozen.

IRQs (by name from the chip YAML): "<name>_ORed_Message_buffer" = IFLAG1 & IMASK1; "<name>_Bus_Off" = BOFFINT &
BOFFMSK; "<name>_Error" = ERRINT & ERRMSK; "<name>_Tx_Warning" = TWRNINT & TWRNMSK; "<name>_Rx_Warning" = RWRNINT &
RWRNMSK (level lines).

Stop modes (`clock_stop`, 46.4.10.3): LPMACK/NOTRDY, TIMER held, Rx ignored, self wake (SLFWAK -> ESR1.WAKINT,
"Wake_Up" interrupt with WAKMSK) on the first frame seen on the bus (at its end, not at its SOF), resync on exit.
Not modelled: SUPV / supervisor access errors, doze mode, the wake-up filter (CTRL2/MCR.WAKSRC), the BUSY
code bit (move-in is instantaneous), CTRL1.SMP / RJW (only affect sampling), TASD (arbitration is instantaneous at
the end of the previous frame), CRC/stuff/form errors of corrupted frames (frames on the emulated bus are never
corrupted), overload frames, CTRL2.WRMFRZ, ESR2 LPTM beyond the simple "lowest inactive / highest priority" rule.
"""
from __future__ import annotations

from collections import deque
from typing import Any

from ...canbus import BUS_IDLE_BITS, BUSOFF_RECOVERY_BITS, CanBus, CanErrorCounters, CanFrame, pin_reason
from ..base import peek_stored as _stored
from ..registry import register
from ._util import GatedBlock, build_regs, find_model, irq_number
from .clockutil import bus_hz

# MCR
MDIS, FRZ, RFEN, HALT, NOTRDY, WAKMSK, SOFTRST, FRZACK = 1 << 31, 1 << 30, 1 << 29, 1 << 28, 1 << 27, 1 << 26, \
    1 << 25, 1 << 24
SUPV, SLFWAK, WRNEN, LPMACK, WAKSRC, SRXDIS, IRMQ = 1 << 23, 1 << 22, 1 << 21, 1 << 20, 1 << 19, 1 << 17, 1 << 16
LPRIOEN, AEN = 1 << 13, 1 << 12
MCR_RO = NOTRDY | FRZACK | LPMACK | (1 << 18) | 0xCC80
MCR_FRZ_ONLY = RFEN | SUPV | WRNEN | WAKSRC | SRXDIS | IRMQ | LPRIOEN | AEN | 0x0300 | 0x7F
# CTRL1
BOFFMSK, ERRMSK, CLKSRC, LPB, TWRNMSK, RWRNMSK = 1 << 15, 1 << 14, 1 << 13, 1 << 12, 1 << 11, 1 << 10
SMP, BOFFREC, TSYN, LBUF, LOM = 1 << 7, 1 << 6, 1 << 5, 1 << 4, 1 << 3
CTRL1_FRZ_ONLY = 0xFFFF0000 | LPB | SMP | TSYN | LBUF | LOM | 0x7
# ESR1
SYNCH, TWRNINT, RWRNINT = 1 << 18, 1 << 17, 1 << 16
BIT1ERR, BIT0ERR, ACKERR, CRCERR, FRMERR, STFERR = 1 << 15, 1 << 14, 1 << 13, 1 << 12, 1 << 11, 1 << 10
TXWRN, RXWRN, IDLE, TX, RX, BOFFINT, ERRINT, WAKINT = 1 << 9, 1 << 8, 1 << 7, 1 << 6, 1 << 3, 1 << 2, 1 << 1, 1
ESR1_W1C = TWRNINT | RWRNINT | BOFFINT | ERRINT | WAKINT
ESR1_ERRBITS = 0xFC00
# CTRL2
EACEN, RRS, MRP = 1 << 16, 1 << 17, 1 << 18
# MB codes
C_RX_INACTIVE, C_EMPTY, C_FULL, C_OVERRUN, C_RANSWER = 0x0, 0x4, 0x2, 0x6, 0xA
C_TX_INACTIVE, C_ABORT, C_DATA, C_TANSWER = 0x8, 0x9, 0xC, 0xE
RX_ACTIVE = (C_EMPTY, C_FULL, C_OVERRUN)
TX_ACTIVE = (C_DATA, C_TANSWER)
FIFO_DEPTH = 6
MB_BASE = 0x80


@register("kinetis.flexcan")
class FlexCAN(GatedBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        digits = "".join(ch for ch in name if ch.isdigit())
        self.index = int(params.get("index", digits or 0))
        self.num_mb = int(params.get("mailboxes", 16))
        self.gate = params.get("gate") or f"CAN{self.index}"
        self.errors = CanErrorCounters()
        self._fifo: deque = deque()
        self._abort_pending: set[int] = set()
        self._serviced: set[int] = set()
        self._locked: int | None = None
        self._lock_counts = False
        self._pending_movein: dict[int, tuple] = {}
        self._sync_t = 0.0
        self._sync_ev = None
        self._boff_ev = None
        self._boff_t = 0.0
        self._tmr = (0, 0.0, False, 0.0)          # (value, base time, running, bit ns)
        self._lp_stop: str | None = None           # Stop mode acknowledged (46.4.10.3): the mode, else None
        self._lp_wake = False                      # MCR.SLFWAK at the stop entry: self wake mechanism armed
        self._lp_frz = False                       # stop entered from freeze mode (FRZACK negated, LPMACK set)
        self._lines: dict[str, bool] = {}
        self._irqs: dict[str, int | None] = {}
        self._pins: dict = {}
        self.tx_frames = self.rx_frames = self.rx_lost = 0
        ov: dict[str, dict] = {
            "MCR": dict(ro_mask=MCR_RO, on_write=self._mcr_write),
            "CTRL1": dict(on_write=self._ctrl1_write),
            "CTRL2": dict(on_write=self._frz_guard(0xFFFFFFFF)),
            "TIMER": dict(ro_mask=0xFFFF0000, on_read=self._timer_read, on_write=self._timer_write,
                          on_peek=lambda b, r: self._timer_value(self.now_ns())),
            "RXMGMASK": dict(on_write=self._frz_guard(0xFFFFFFFF)),
            "RX14MASK": dict(on_write=self._frz_guard(0xFFFFFFFF)),
            "RX15MASK": dict(on_write=self._frz_guard(0xFFFFFFFF)),
            "RXFGMASK": dict(on_write=self._frz_guard(0xFFFFFFFF)),
            "ECR": dict(on_read=self._ecr_read, on_write=self._ecr_write, on_peek=self._ecr_read),
            "ESR1": dict(ro_mask=0xFFFFFFFF & ~ESR1_W1C, w1c_mask=ESR1_W1C, on_read=self._esr1_read,
                         on_write=self._irq_write, on_peek=self._esr1_value),
            "IMASK1": dict(on_write=self._irq_write),
            "IFLAG1": dict(w1c_mask=0xFFFFFFFF, on_write=self._iflag_write),
            "ESR2": dict(ro_mask=0xFFFFFFFF, on_read=self._esr2_read, on_peek=self._esr2_read),
            "CRCR": dict(ro_mask=0xFFFFFFFF),
            "RXFIR": dict(ro_mask=0xFFFFFFFF),
        }
        for n in range(self.num_mb):
            # message buffers are RAM (Rx FIFO filter elements overlay them): no SVD reserved-bit masks
            ov[f"CS{n}"] = dict(ro_mask=0, on_read=self._cs_reader(n), on_write=self._cs_writer(n),
                                on_peek=_stored)     # a read locks the MB: a peek does not
            ov[f"ID{n}"] = dict(ro_mask=0)
            ov[f"WORD0{n}"] = dict(ro_mask=0)
            ov[f"WORD1{n}"] = dict(ro_mask=0)
            ov[f"RXIMR{n}"] = dict(on_write=self._frz_guard(0xFFFFFFFF))
        r = build_regs((name, f"CAN{self.index}", "CAN0"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self._o = {k: self.regs[k].boff for k in ("MCR", "CTRL1", "CTRL2", "ESR1", "IMASK1", "IFLAG1", "CRCR",
                                                   "RXFIR", "RXMGMASK", "RX14MASK", "RX15MASK", "RXFGMASK")}
        self.rx_signal = f"CAN{self.index}_RX"
        self.tx_signal = f"CAN{self.index}_TX"
        self._own_bus = CanBus(name)
        self._loop_bus = CanBus(f"{name}.loopback", self_ack=True)
        self.can_bus: CanBus = self._own_bus
        self._own_bus.attach(self)
        self._loop_bus.attach(self)

    # ------------------------------------------------------------------ setup
    def attach(self, chip: Any) -> None:
        super().attach(chip)
        for key, suffix in (("mb", "ORed_Message_buffer"), ("boff", "Bus_Off"), ("err", "Error"),
                            ("twrn", "Tx_Warning"), ("rwrn", "Rx_Warning"), ("wake", "Wake_Up")):
            self._irqs[key] = irq_number(chip, f"{self.name}_{suffix}") or irq_number(chip, f"CAN{self.index}_{suffix}")
        self._sim = find_model(chip, "kinetis.sim", "SIM")

    def reset(self) -> None:
        if self.chip is not None:
            for ev in (self._sync_ev, self._boff_ev):
                self.chip.sched.cancel(ev)
            self.can_bus.node_reset(self)
            self._loop_bus.node_reset(self)
        self._sync_ev = self._boff_ev = None
        for key, lvl in list(self._lines.items()):
            if lvl:
                self._set_line(key, False)
        super().reset()
        self._soft_state()
        self._tmr = (0, self.now_ns(), False, 0.0)
        self._lp_stop, self._lp_wake, self._lp_frz = None, False, False

    def _soft_state(self) -> None:
        self.errors.reset()
        self._fifo.clear()
        self._abort_pending.clear()
        self._serviced.clear()
        self._locked = None
        self._pending_movein.clear()
        self._sync_t = float("inf")

    # ------------------------------------------------------------------ CAN node protocol (chipem.canbus)
    @property
    def can_name(self) -> str:
        return self.name

    def can_attach(self, bus: CanBus) -> None:
        """Join a board bus (chipem.board.CanLink); `can_detach()` goes back to the private bus."""
        if bus is self.can_bus:
            return
        self.can_bus.detach(self)
        self.can_bus = bus
        bus.attach(self)

    def can_detach(self) -> None:
        if self.can_bus is not self._own_bus:
            self.can_bus.detach(self)
            self.can_bus = self._own_bus
            self._own_bus.attach(self)

    def _bus(self) -> CanBus:
        return self._loop_bus if self.get("CTRL1") & LPB else self.can_bus

    def clock_hz(self) -> float:
        if self.chip is None:
            return 0.0
        if self.get("CTRL1") & CLKSRC:
            return bus_hz(self.chip)
        mcg = self.chip.peripherals.get("MCG")
        hz = float(getattr(mcg, "ext_hz", 0) or 0) if mcg is not None else float(self.params.get("osc_hz", 8e6))
        osc = find_model(self.chip, "kinetis.osc")
        if osc is not None and "CR" in osc.regs and not osc.get("CR") & 0x80:
            return 0.0                                       # OSC0_CR.ERCLKEN = 0: OSCERCLK off
        return hz

    def can_bit_ns(self) -> float:
        c1 = self.get("CTRL1")
        hz = self.clock_hz()
        if hz <= 0:
            return 0.0
        tq = ((c1 >> 24) + 1) / hz
        return 1e9 * tq * (1 + ((c1 & 7) + 1) + (((c1 >> 19) & 7) + 1) + (((c1 >> 16) & 7) + 1))

    def bitrate(self) -> float:
        b = self.can_bit_ns()
        return 1e9 / b if b > 0 else 0.0

    def _running(self) -> bool:
        mcr = self.get("MCR")
        return not mcr & MDIS and not (mcr & FRZ and mcr & HALT)

    def _gate_on(self) -> bool:
        sim = self._sim
        return sim is None or sim.clock_enabled(self.gate)

    def _on_bus(self, bus: CanBus) -> bool:
        if bus is not self._bus() or not self._running() or self.errors.busoff or not self._gate_on():
            return False
        return self.chip.now_ns() >= self._sync_t

    def can_tx_frame(self, bus: CanBus) -> CanFrame | None:
        if not self._on_bus(bus) or self.get("CTRL1") & LOM:
            return None
        n = self._best_tx()
        return None if n is None else self._frame_of(n)

    def can_tx_connected(self, bus: CanBus) -> bool:
        if bus is self._loop_bus:
            return True
        return pin_reason(self.chip, self.tx_signal, self._pins) is None and \
            pin_reason(self.chip, self.rx_signal, self._pins) is None

    def can_rx_state(self, bus: CanBus) -> str | None:
        if self._lp_stop is not None:                  # Stop: Rx input ignored; self wake watches the bus
            if not self._lp_wake or bus is not self.can_bus or self.get("CTRL1") & LPB:
                return None
            return "listen" if pin_reason(self.chip, self.rx_signal, self._pins) is None else None
        if not self._on_bus(bus):
            return None
        if bus is not self._loop_bus and pin_reason(self.chip, self.rx_signal, self._pins) is not None:
            return None
        return "listen" if self.get("CTRL1") & LOM else "active"

    def can_error_passive(self) -> bool:
        return self.errors.state == "passive"

    def can_rx(self, bus: CanBus, frame: CanFrame, t_sof: float, t: float) -> None:
        if self.can_rx_state(bus) is None:
            return
        if self._lp_stop is not None:
            self._self_wake()
            return
        if not self.get("CTRL1") & LOM:
            self._err_events(self.errors.rx_ok())
        self.rx_frames += 1
        self._rx_match(frame, t_sof)

    def can_rx_error(self, bus: CanBus, kind: str, t: float) -> None:
        if self.can_rx_state(bus) is None:
            return
        if self._lp_stop is not None:
            self._self_wake()
            return
        if kind == "noack":
            self._err_flag(BIT0ERR)              # listen-only: "flags a BIT0 error without changing the REC"
            return
        self._err_flag(FRMERR)
        if not self.get("CTRL1") & LOM:
            self._err_events(self.errors.rx_error())

    def can_tx_done(self, bus: CanBus, frame: CanFrame, ok: bool, err: str | None, t_sof: float, t: float) -> None:
        n = frame.tag
        if ok:
            self.tx_frames += 1
            self._err_events(self.errors.tx_ok())
            if bus is self._loop_bus or not self.get("MCR") & SRXDIS:
                lost = self.rx_lost
                self._rx_match(frame, t_sof)            # self reception (before the Tx MB changes its code)
                self.rx_lost = lost                     # an own frame nobody wants is not "lost"
            if n is not None and self._code(n) in TX_ACTIVE:
                code = self._code(n)
                new = C_RANSWER if code == C_TANSWER else (C_EMPTY if frame.rtr else C_TX_INACTIVE)
                cs = self._cs(n)
                self._set_cs(n, (cs & ~0x0F00FFFF) | (new << 24) | self._timer_at(t_sof, 2))
                self.set("CRCR", ((n & 0x7F) << 16) | frame.crc)
                self._abort_pending.discard(n)
                self._set_iflag(n)
        else:
            self._err_flag(ACKERR if err == "ack" else BIT0ERR)
            self._err_events(self.errors.tx_error(err or "bit"))
            if n is not None and n in self._abort_pending:
                self._abort_pending.discard(n)
                cs = self._cs(n)
                self._set_cs(n, (cs & ~0x0F000000) | (C_ABORT << 24))
                self._set_iflag(n)
        self._tx_changed()

    # ------------------------------------------------------------------ helpers
    def _frz_guard(self, mask: int):
        def hook(blk, reg, old, new):
            if self._cfg_ok():
                return None
            return (old & mask) | (new & ~mask)
        return hook

    def _cfg_ok(self) -> bool:
        mcr = self.get("MCR")
        return bool(mcr & MDIS or (mcr & FRZ and mcr & HALT))

    def _first_mb(self) -> int:
        if self.get("MCR") & RFEN:
            return 8 + 2 * ((self.get("CTRL2") >> 24) & 0xF)
        return 0

    def _last_mb(self) -> int:
        return min(self.get("MCR") & 0x7F, self.num_mb - 1)

    def _cs(self, n: int) -> int:
        o = MB_BASE + 16 * n
        return int.from_bytes(self.mem[o:o + 4], "little")

    def _set_cs(self, n: int, v: int) -> None:
        o = MB_BASE + 16 * n
        self.mem[o:o + 4] = (v & 0xFFFFFFFF).to_bytes(4, "little")

    def _word(self, off: int) -> int:
        return int.from_bytes(self.mem[off:off + 4], "little")

    def _set_word(self, off: int, v: int) -> None:
        self.mem[off:off + 4] = (v & 0xFFFFFFFF).to_bytes(4, "little")

    def _code(self, n: int) -> int:
        return (self._cs(n) >> 24) & 0xF

    def _frame_of(self, n: int) -> CanFrame:
        o = MB_BASE + 16 * n
        cs, idw = self._cs(n), self._word(o + 4)
        ext = bool(cs >> 21 & 1)
        ident = idw & 0x1FFFFFFF if ext else (idw >> 18) & 0x7FF
        data = self._word(o + 8).to_bytes(4, "big") + self._word(o + 12).to_bytes(4, "big")
        return CanFrame(ident, data, ext=ext, rtr=bool(cs >> 20 & 1), dlc=(cs >> 16) & 0xF, tag=n)

    def _best_tx(self) -> int | None:
        lbuf = self.get("CTRL1") & LBUF
        prio = self.get("MCR") & LPRIOEN
        best, bkey = None, None
        for n in range(self._first_mb(), self._last_mb() + 1):
            if self._code(n) not in TX_ACTIVE:
                continue
            if lbuf:
                return n
            key = self._frame_of(n).arb
            if prio:
                key |= ((self._word(MB_BASE + 16 * n + 4) >> 29) & 7) << 32
            if bkey is None or key < bkey:
                best, bkey = n, key
        return best

    def _tx_changed(self) -> None:
        if self.chip is None:
            return
        bus = self._bus()
        if self._on_bus(bus) and not self.get("CTRL1") & LOM and self._best_tx() is not None:
            bus.request(self)
        else:
            bus.withdraw(self)

    # ------------------------------------------------------------------ low-power modes (SMC)
    def clock_stop(self, stopped: bool, mode: str) -> None:
        """Stop / VLPS / LLS (46.4.10.3, Table 7-2: CAN "wakeup" in Stop/VLPS, static in LLS).  A frame of ours on
        the wire completes (the bus engine finishes it), then the module ignores its Rx input, drives Tx recessive
        (no requests), sets MCR.NOTRDY and LPMACK and its free-running TIMER stops.  Stop entered from freeze mode
        sets LPMACK and negates FRZACK.  Self wake (MCR.SLFWAK at the stop entry, Stop/VLPS only): bus activity sets
        ESR1.WAKINT (interrupt "Wake_Up" with MCR.WAKMSK), which wakes the core; the frame itself is not received.
        Exit: LPMACK/NOTRDY clear and the module resynchronises to the bus (11 recessive bits).  WAIT/VLPW: the K22
        FlexCAN has no MCR.DOZE, nothing happens."""
        if self.chip is None or mode in ("WAIT", "VLPW"):
            return
        if stopped:
            mcr = self.get("MCR")
            if mcr & MDIS:
                return                                       # disabled: already in its low-power state
            now = self.now_ns()
            self._tmr = (self._timer_value(now), now, False, self._tmr[3])
            self._lp_stop = mode
            self._lp_frz = not self._running()
            self._lp_wake = bool(mcr & SLFWAK) and mode in ("STOP", "VLPS")
            self.set("MCR", (mcr | LPMACK | NOTRDY) & ~FRZACK)
            self._sync_t = float("inf")
            self.chip.sched.cancel(self._sync_ev)
            self._sync_ev = None
            self._bus().withdraw(self)
            return
        if self._lp_stop is None:
            return
        self._lp_stop = None
        self._lp_wake = False
        self.set("MCR", self._mcr_status(self.get("MCR")))
        self._timer_rebase()
        if self._running():
            self._start_sync()
        self._update_irq()

    def _self_wake(self) -> None:
        """Recessive-to-dominant transition seen in Stop with self wake armed (46.4.10.3)."""
        self._lp_wake = False
        self._store_bits("ESR1", WAKINT, WAKINT)
        self._update_irq()

    # ------------------------------------------------------------------ timer
    def _timer_value(self, t: float) -> int:
        v, t0, run, bit = self._tmr
        if not run or bit <= 0:
            return v
        return (v + int(max(0.0, t - t0) // bit)) & 0xFFFF

    def _timer_at(self, t_sof: float, bits: int) -> int:
        v, t0, run, bit = self._tmr
        return self._timer_value(t_sof + bits * bit) if run and bit > 0 else v

    def _timer_rebase(self, value: int | None = None) -> None:
        now = self.now_ns()
        v = self._timer_value(now) if value is None else value & 0xFFFF
        self._tmr = (v, now, self._running(), self.can_bit_ns())

    def _timer_read(self, blk, reg):
        self._unlock()                          # global unlock (§46.4.6.3)
        return self._timer_value(self.now_ns())

    def _timer_write(self, blk, reg, old, new):
        self._timer_rebase(new)
        return None

    # ------------------------------------------------------------------ MCR / CTRL1
    def _mcr_write(self, blk, reg, old, new):
        if not self._cfg_ok_value(old):
            new = (new & ~MCR_FRZ_ONLY) | (old & MCR_FRZ_ONLY)
        if not (new & WRNEN):
            self._store_bits("CTRL1", 0, TWRNMSK | RWRNMSK)
        if new & SOFTRST:
            new = (self.regs["MCR"].reset & ~MDIS) | (new & MDIS)
            for rn in ("TIMER", "ECR", "ESR1", "ESR2", "IMASK1", "IFLAG1", "CRCR"):
                self.set(rn, self.regs[rn].reset)
            if self.chip is not None:
                self._bus().node_reset(self)
            self._soft_state()
        if (old ^ new) & RFEN:
            self._fifo.clear()
            self._store_bits("IFLAG1", 0, 0xFF)       # "cleared by the FlexCAN whenever RFEN is changed"
        was = self._run_value(old)
        new = self._mcr_status(new)
        self.set("MCR", new)
        now_run = self._running()
        if was != now_run:
            self._timer_rebase()
            if now_run:
                self._start_sync()
            else:
                self._sync_t = float("inf")
                if self.chip is not None:
                    self.chip.sched.cancel(self._sync_ev)
                    self._sync_ev = None
                    self._bus().withdraw(self)
        self._update_irq()
        return new

    @staticmethod
    def _cfg_ok_value(mcr: int) -> bool:
        return bool(mcr & MDIS or (mcr & FRZ and mcr & HALT))

    @staticmethod
    def _run_value(mcr: int) -> bool:
        return not mcr & MDIS and not (mcr & FRZ and mcr & HALT)

    @staticmethod
    def _mcr_status(v: int) -> int:
        v &= ~(NOTRDY | FRZACK | LPMACK | SOFTRST)
        if v & MDIS:
            v |= LPMACK | NOTRDY
        elif v & FRZ and v & HALT:
            v |= FRZACK | NOTRDY
        return v

    def _store_bits(self, regname: str, value: int, mask: int) -> None:
        self.set(regname, (self.get(regname) & ~mask) | (value & mask))

    def _start_sync(self) -> None:
        if self.chip is None:
            return
        bit = self.can_bit_ns()
        self.chip.sched.cancel(self._sync_ev)
        self._sync_ev = None
        if bit <= 0:
            self._sync_t = float("inf")         # no bit clock: it never synchronises
            return
        self._sync_t = self.now_ns() + BUS_IDLE_BITS * bit
        self._sync_ev = self.chip.sched.at_ns(self._sync_t, self._synced)

    def _synced(self) -> None:
        self._sync_ev = None
        self._tx_changed()

    def _ctrl1_write(self, blk, reg, old, new):
        mcr = self.get("MCR")
        if not self._cfg_ok_value(mcr):
            new = (new & ~CTRL1_FRZ_ONLY) | (old & CTRL1_FRZ_ONLY)
        if not mcr & MDIS:
            new = (new & ~CLKSRC) | (old & CLKSRC)
        if not mcr & WRNEN:
            new &= ~(TWRNMSK | RWRNMSK)
        self.set("CTRL1", new)
        if (old ^ new) & LPB and self.chip is not None:
            (self.can_bus if new & LPB else self._loop_bus).withdraw(self)
        if (old ^ new) & 0xFFFF2007:
            self._timer_rebase()
        if old & BOFFREC and not new & BOFFREC and self.errors.busoff and self.chip is not None:
            bit = self.can_bit_ns()
            t = max(self._boff_t + BUSOFF_RECOVERY_BITS * bit, self.now_ns() + BUS_IDLE_BITS * bit)
            self._sched_recovery(t)
        self._update_irq()
        return new

    # ------------------------------------------------------------------ errors
    def _ecr_read(self, blk, reg):
        return ((self.errors.rec & 0xFF) << 8) | (self.errors.tec & 0xFF)

    def _ecr_write(self, blk, reg, old, new):
        if self._cfg_ok():
            self._err_events(self.errors.set(new & 0xFF, (new >> 8) & 0xFF))
        return 0

    def _esr1_read(self, blk, reg):
        v = self._esr1_value(blk, reg)
        o = self._o["ESR1"]
        stored = int.from_bytes(self.mem[o:o + 4], "little")
        if stored & ESR1_ERRBITS:                  # error bits 15-10 clear on read
            self.mem[o:o + 4] = (stored & ~ESR1_ERRBITS).to_bytes(4, "little")
        return v

    def _esr1_value(self, blk, reg):
        """ESR1 as a read returns it (no clear-on-read: also the debugger peek)."""
        o = self._o["ESR1"]
        stored = int.from_bytes(self.mem[o:o + 4], "little")
        v = stored
        st = "passive" if self.get("CTRL1") & LOM and not self.errors.busoff else self.errors.state
        v |= {"active": 0, "passive": 1 << 4, "busoff": 2 << 4}[st]
        if self.errors.tec >= 96 and not self.errors.busoff:
            v |= TXWRN
        if self.errors.rec >= 96:
            v |= RXWRN
        if self.chip is not None and self._running():
            now = self.now_ns()
            if now >= self._sync_t:
                v |= SYNCH
                act = self._bus().activity(self, now)
                v |= TX if act == "tx" else RX if act == "rx" else IDLE
        return v

    def _esr2_read(self, blk, reg):
        first, last = self._first_mb(), self._last_mb()
        inactive = [n for n in range(first, last + 1) if self._code(n) in (C_RX_INACTIVE, C_TX_INACTIVE)]
        if inactive:
            return (1 << 14) | (1 << 13) | (inactive[0] << 16)
        n = self._best_tx()
        return ((1 << 14) | (n << 16)) if n is not None else 0

    def _err_flag(self, bits: int) -> None:
        o = self._o["ESR1"]
        v = int.from_bytes(self.mem[o:o + 4], "little") | bits | ERRINT
        self.mem[o:o + 4] = v.to_bytes(4, "little")
        self._update_irq()

    def _err_events(self, evs: set[str]) -> None:
        if not evs:
            return
        o = self._o["ESR1"]
        v = int.from_bytes(self.mem[o:o + 4], "little")
        if self.get("MCR") & WRNEN:
            if "tx_warn" in evs:
                v |= TWRNINT
            if "rx_warn" in evs:
                v |= RWRNINT
        if "busoff" in evs:
            v |= BOFFINT
            self._boff_t = self.now_ns()
            self.emit("info", block=self.name, msg=f"{self.name}: bus off (TEC > 255)")
            if self.chip is not None:
                self._bus().withdraw(self)
                if not self.get("CTRL1") & BOFFREC:
                    self._sched_recovery(self.now_ns() + BUSOFF_RECOVERY_BITS * self.can_bit_ns())
        self.mem[o:o + 4] = v.to_bytes(4, "little")
        self._update_irq()

    def _sched_recovery(self, t: float) -> None:
        self.chip.sched.cancel(self._boff_ev)
        self._boff_ev = self.chip.sched.at_ns(t, self._recovered)

    def _recovered(self) -> None:
        self._boff_ev = None
        if self.errors.busoff:
            self._err_events(self.errors.recover())
            self.emit("info", block=self.name, msg=f"{self.name}: bus off recovery, error active again")
            self._tx_changed()

    # ------------------------------------------------------------------ interrupts
    def _set_line(self, key: str, level: bool) -> None:
        if self._lines.get(key, False) == level:
            return
        self._lines[key] = level
        n = self._irqs.get(key)
        if n is not None and self.chip is not None:
            self.irq(n, level)

    def _update_irq(self) -> None:
        if not hasattr(self, "_o"):
            return
        c1, esr = self.get("CTRL1"), self.get("ESR1")
        self._set_line("mb", bool(self.get("IFLAG1") & self.get("IMASK1")))
        self._set_line("boff", bool(esr & BOFFINT and c1 & BOFFMSK))
        self._set_line("err", bool(esr & ERRINT and c1 & ERRMSK))
        self._set_line("twrn", bool(esr & TWRNINT and c1 & TWRNMSK))
        self._set_line("rwrn", bool(esr & RWRNINT and c1 & RWRNMSK))
        self._set_line("wake", bool(esr & WAKINT and self.get("MCR") & WAKMSK))

    def _irq_write(self, blk, reg, old, new):
        self._update_irq()
        return None

    def _set_iflag(self, n: int) -> None:
        self.set("IFLAG1", self.get("IFLAG1") | (1 << n))
        self._update_irq()

    def _iflag_write(self, blk, reg, old, new):
        if self.get("MCR") & RFEN and old & (1 << 5) and not new & (1 << 5):
            if self._fifo:
                self._fifo.popleft()
            if self._fifo:
                self._fifo_output()
                new |= 1 << 5
        self.set("IFLAG1", new)
        self._update_irq()
        return new

    # ------------------------------------------------------------------ message buffers (CPU side)
    def _unlock(self) -> None:
        n = self._locked
        if n is None:
            return
        self._locked = None
        if self._lock_counts and self._code(n) in (C_FULL, C_OVERRUN):
            self._serviced.add(n)
        p = self._pending_movein.pop(n, None)
        if p is not None:
            self._move_in(n, *p)

    def _cs_reader(self, n: int):
        def hook(blk, reg):
            if n < self._first_mb():
                return None
            if self._locked is not None and self._locked != n:
                self._unlock()
            code = self._code(n)
            if code in (C_FULL, C_OVERRUN) or (code == C_EMPTY and not self.get("MCR") & IRMQ):
                self._locked = n
                self._lock_counts = code in (C_FULL, C_OVERRUN)
            return None
        return hook

    def _cs_writer(self, n: int):
        def hook(blk, reg, old, new):
            if n < self._first_mb():
                return None
            if self._locked == n:
                self._locked = None              # a C/S write unlocks and cancels a pending move-in
            self._pending_movein.pop(n, None)
            self._serviced.discard(n)
            oc, nc = (old >> 24) & 0xF, (new >> 24) & 0xF
            if oc in TX_ACTIVE and nc == C_ABORT and self.get("MCR") & AEN:
                fr = self._bus().transmitting(self) if self.chip is not None else None
                if fr is not None and fr.tag == n:
                    self._abort_pending.add(n)   # on the bus: write blocked, abort kept pending
                    return old
                self._set_cs(n, new)
                self._set_iflag(n)               # inactivated before transmission: ABORT code + IFLAG
            if oc in TX_ACTIVE or nc in TX_ACTIVE:
                self._set_cs(n, new)
                self._tx_changed()
            return None
        return hook

    # ------------------------------------------------------------------ reception
    def _mask_for(self, n: int) -> int:
        if self.get("MCR") & IRMQ:
            return self.get(f"RXIMR{n}") if n < self.num_mb else 0xFFFFFFFF
        if n == 14:
            return self.get("RX14MASK")
        if n == 15:
            return self.get("RX15MASK")
        return self.get("RXMGMASK")

    def _mb_match(self, n: int, fr: CanFrame) -> bool:
        cs = self._cs(n)
        idw = self._word(MB_BASE + 16 * n + 4) & 0x1FFFFFFF
        mask = self._mask_for(n)
        fid = fr.id if fr.ext else fr.id << 18
        if (idw ^ fid) & mask & (0x1FFFFFFF if fr.ext else 0x1FFC0000):
            return False
        mb_ide, mb_rtr = bool(cs >> 21 & 1), bool(cs >> 20 & 1)
        if self.get("CTRL2") & EACEN:
            if mask >> 30 & 1 and mb_ide != fr.ext:
                return False
            if mask >> 31 & 1 and mb_rtr != fr.rtr:
                return False
            return True
        return mb_ide == fr.ext

    def _free(self, n: int) -> bool:
        code = self._code(n)
        return code == C_EMPTY or (code in (C_FULL, C_OVERRUN) and n in self._serviced and self._locked != n)

    def _rx_match(self, fr: CanFrame, t_sof: float) -> None:
        mcr, c2 = self.get("MCR"), self.get("CTRL2")
        first, last = self._first_mb(), self._last_mb()
        fifo = bool(mcr & RFEN)
        if fr.rtr and not c2 & RRS:
            for n in range(first, last + 1):        # remote request -> RANSWER MB with the same ID (no masks)
                if self._code(n) != C_RANSWER:
                    continue
                cs = self._cs(n)
                idw = self._word(MB_BASE + 16 * n + 4) & 0x1FFFFFFF
                if bool(cs >> 21 & 1) == fr.ext and idw == (fr.id if fr.ext else fr.id << 18) & \
                        (0x1FFFFFFF if fr.ext else 0x1FFC0000):
                    self._set_cs(n, (cs & ~0x0F000000) | (C_TANSWER << 24))
                    self._tx_changed()
                    return
            if fifo:
                self._fifo_offer(fr, t_sof, mb_fallback=None)
            return
        matched = [n for n in range(first, last + 1) if self._code(n) in RX_ACTIVE and self._mb_match(n, fr)]
        free = next((n for n in matched if self._free(n)), None)
        if mcr & IRMQ:
            mb = free if free is not None else (matched[-1] if matched else None)
        else:
            mb = matched[0] if matched else None
        if fifo:
            hit = self._fifo_filter(fr)
            if c2 & MRP:                              # mailboxes first
                if free is not None:
                    return self._move_in(free, fr, t_sof)
                if hit is not None and len(self._fifo) < FIFO_DEPTH:
                    return self._fifo_push(fr, t_sof, hit)
                if mb is not None:
                    return self._move_in(mb, fr, t_sof)
                if hit is not None:
                    return self._fifo_overflow()
                self.rx_lost += 1
                return
            if hit is not None and len(self._fifo) < FIFO_DEPTH:
                return self._fifo_push(fr, t_sof, hit)
            if mb is not None:
                return self._move_in(mb, fr, t_sof)
            if hit is not None:
                return self._fifo_overflow()
            self.rx_lost += 1
            return
        if mb is None:
            self.rx_lost += 1                         # no matching MB: the frame is not stored
            return
        self._move_in(mb, fr, t_sof)

    def _move_in(self, n: int, fr: CanFrame, t_sof: float) -> None:
        if self._locked == n:
            self._pending_movein[n] = (fr, t_sof)     # newest pending frame replaces an older one
            return
        code = self._code(n)
        if code not in RX_ACTIVE:
            return                                    # inactivated meanwhile
        new = C_FULL if code == C_EMPTY or n in self._serviced else C_OVERRUN
        self._serviced.discard(n)
        cs = (new << 24) | ((1 << 22) if fr.ext else 0) | (int(fr.ext) << 21) | (int(fr.rtr) << 20) | \
            (fr.dlc << 16) | self._timer_at(t_sof, 2)
        o = MB_BASE + 16 * n
        self._set_cs(n, cs)
        self._set_word(o + 4, (self._word(o + 4) & 0xE0000000) | (fr.id if fr.ext else fr.id << 18))
        data = fr.data + bytes(8 - len(fr.data))
        self._set_word(o + 8, int.from_bytes(data[:4], "big"))
        self._set_word(o + 12, int.from_bytes(data[4:], "big"))
        if self.get("CTRL1") & TSYN and n == self._first_mb():
            self._timer_rebase(0)
        self._set_iflag(n)

    # ------------------------------------------------------------------ Rx FIFO
    def _fifo_filter(self, fr: CanFrame) -> int | None:
        mcr, c2 = self.get("MCR"), self.get("CTRL2")
        idam = (mcr >> 8) & 3
        if idam == 3:
            return None
        rffn = (c2 >> 24) & 0xF
        n_el = min(8 * (rffn + 1), (self.num_mb - 6) * 4)
        n_imr = min(8 + 2 * rffn, self.num_mb) if mcr & IRMQ else 0
        fgm = self.get("RXFGMASK")
        for k in range(n_el):
            elem = self._word(0xE0 + 4 * k)
            mask = self.get(f"RXIMR{k}") if k < n_imr else fgm
            if idam == 0:
                fw = (int(fr.rtr) << 31) | (int(fr.ext) << 30) | (fr.id << 1 if fr.ext else fr.id << 19)
                cm = mask & (0xFFFFFFFE if fr.ext else 0xFFF80000)
                if not (elem ^ fw) & cm:
                    return k
            elif idam == 1:
                fw = (int(fr.rtr) << 15) | (int(fr.ext) << 14) | ((fr.id >> 15) & 0x3FFF if fr.ext else
                                                                  (fr.id << 3) & 0x3FF8)
                for h, sh in ((0, 16), (1, 0)):
                    cm = (mask >> sh) & (0xFFFF if fr.ext else 0xFFF8)
                    if not (((elem >> sh) & 0xFFFF) ^ fw) & cm:
                        return 2 * k + h
            else:
                fw = (fr.id >> 21) & 0xFF if fr.ext else (fr.id >> 3) & 0xFF
                for j in range(4):
                    sh = 24 - 8 * j
                    if not (((elem >> sh) & 0xFF) ^ fw) & ((mask >> sh) & 0xFF):
                        return 4 * k + j
        return None

    def _fifo_offer(self, fr: CanFrame, t_sof: float, mb_fallback=None) -> None:
        hit = self._fifo_filter(fr)
        if hit is None:
            self.rx_lost += 1
        elif len(self._fifo) < FIFO_DEPTH:
            self._fifo_push(fr, t_sof, hit)
        else:
            self._fifo_overflow()

    def _fifo_push(self, fr: CanFrame, t_sof: float, hit: int) -> None:
        self._fifo.append((fr, hit, self._timer_at(t_sof, 2)))
        if len(self._fifo) == 1:
            self._fifo_output()
            self._set_iflag(5)
        elif len(self._fifo) == 5:
            self._set_iflag(6)                      # almost full (4 -> 5 unread frames)

    def _fifo_overflow(self) -> None:
        self.rx_lost += 1
        self._set_iflag(7)

    def _fifo_output(self) -> None:
        fr, hit, ts = self._fifo[0]
        self._set_word(0x80, ((1 << 22) if fr.ext else 0) | (int(fr.ext) << 21) | (int(fr.rtr) << 20) |
                       (fr.dlc << 16) | ts)
        self._set_word(0x84, fr.id if fr.ext else fr.id << 18)
        data = fr.data + bytes(8 - len(fr.data))
        self._set_word(0x88, int.from_bytes(data[:4], "big"))
        self._set_word(0x8C, int.from_bytes(data[4:], "big"))
        self.set("RXFIR", hit & 0x1FF)

    # ------------------------------------------------------------------ reporting
    def can_stats(self) -> dict:
        st = "disabled" if self.get("MCR") & MDIS else "freeze" if self._cfg_ok() else self.errors.state
        return {"bitrate": self.bitrate(), "tx": self.tx_frames, "rx": self.rx_frames, "lost": self.rx_lost,
                "tec": self.errors.tec & 0xFF, "rec": self.errors.rec, "state": st,
                "bus": self.can_bus.name if self.can_bus is not self._own_bus else None}
