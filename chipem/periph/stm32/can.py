"""STM32F4 bxCAN (CAN1 master / CAN2 slave, RM0090 ch. 32 "Controller area network (bxCAN)") on a chipem.canbus bus.

Register tables from the SVD (CAN1/CAN2: MCR, MSR, TSR, RF0R, RF1R, IER, ESR, BTR, TIxR/TDTxR/TDLxR/TDHxR,
RIxR/RDTxR/RDLxR/RDHxR, FMR, FM1R, FS1R, FFA1R, FA1R, FiR1/FiR2); behaviour hand-written.  The bus (bit timing,
arbitration, ACK, error frames) is chipem/canbus.py, shared with kinetis.flexcan and ti_c2000.ecan.

Operating modes (§32.4, Figure 336).  After reset the bxCAN is in Sleep mode (MCR.SLEEP = 1, MSR.SLAK = 1).
* MCR.INRQ = 1 requests Initialization mode (INRQ takes precedence over SLEEP: from Sleep, INRQ alone gives INAK = 1,
  which is what both the HAL (INRQ, then clear SLEEP) and the SPL (clear SLEEP, then INRQ) sequences wait for).  The
  request is granted when the current bus activity (our transmission or a frame we are receiving) is over: INAK = 1.
  Pending transmit requests stay pending.  BTR is writable only in Initialization mode (INAK = 1).
* MCR.SLEEP = 1 with INRQ = 0 requests Sleep mode, granted after the current activity: SLAK = 1 and, with
  IER.SLKIE, MSR.SLAKI.  MCR.AWUM: a frame on the bus while asleep (WKUI, with WKUIE an SCE interrupt) clears SLEEP by
  hardware; the frame that woke the controller is lost (it is not synchronised yet).
* INRQ = 0 and SLEEP = 0: Normal mode once the bxCAN has synchronised: 11 consecutive recessive bits on CANRX after
  the end of the current bus activity, then INAK / SLAK clear.  The RX pin must be muxed to CANx_RX (AF9) for that,
  as on silicon (an unconfigured RX pin never shows bus idle: INAK stays 1, e.g. HAL_CAN_Start times out; one `warn`).
  In loop back mode (BTR.LBKM) the RX pin is disregarded.  CAN2 also needs the CAN1 clock (it is the slave: the
  filters and the master logic are clocked by CAN1).
* MCR.RESET: master reset of this bxCAN (back to Sleep, registers at reset values; the filter banks are kept).
* MCR.DBF (debug freeze) is stored only.

Bit timing (§32.7.7): tq = (BRP + 1) / PCLK1 (APB1 clock from the RCC model); bit = (1 + (TS1 + 1) + (TS2 + 1)) tq.
SJW only affects resynchronisation (not modelled).  BTR.SILM: silent mode, receives without acknowledging and never
starts a transmission (requests stay pending).  BTR.LBKM: loop back mode on a private bus that acknowledges itself
(our frames are received through our own filters; the board bus and the RX pin are not involved -- on silicon the
frames would also appear on CANTX unless SILM is set as well).

Transmission (§32.7.1): three mailboxes.  Writing TIxR.TXRQ = 1 to an empty mailbox (TSR.TMEx = 1) makes it pending;
TIxR/TDTxR/TDLxR/TDHxR are write-protected while pending.  The pending mailbox with the highest priority competes on
the bus: MCR.TXFP = 0 by identifier (the arbitration value: lower ID first, then data before remote, standard before
extended; equal: the lower mailbox), TXFP = 1 in request order.  TSR.CODE = the lowest empty mailbox, or the lowest
priority pending one when none is empty; LOWx = mailbox x has the lowest priority among two or more pending.
Completion: RQCPx = 1 and TXOKx = 1 (TMEx = 1, TXRQ cleared).  A failed attempt (no ACK: ESR.LEC = 3; TX pin not
muxed: the SOF reads back recessive, LEC = 5 bit dominant error) sets TERRx and is retried automatically, unless
MCR.NART = 1: then the mailbox completes with RQCPx = 1, TXOKx = 0, TERRx = 1.  Losing arbitration to another node's
frame sets ALSTx (NART = 1: the mailbox completes with TXOK = 0).  TSR.ABRQx aborts a pending mailbox at once
(RQCP = 1, TXOK = 0); while its frame is on the bus the abort waits for the outcome (success: normal completion).
Writing 1 to RQCPx clears RQCPx, TXOKx, ALSTx and TERRx; so does a new transmit request (TXRQ = 1) on that mailbox
(RM0090 32.9.2 CAN_TSR).
MCR.TTCM: the 16-bit CAN timer counts bit times from the end of synchronisation; TDTxR.TIME / RDTxR.TIME get its
value at the SOF (0 with TTCM = 0).  TGT (TTCM, DLC = 8): bytes 6-7 carry TIME (taken when the frame is chosen for
arbitration, i.e. at most one frame earlier than the real SOF).

Reception (§32.7.4-5): a valid frame (EOF) passes the acceptance filters (below) into FIFO 0 or 1 (FFA1R), three
messages deep each.  RFxR.FMP = number of pending messages, FULL (3 pending, rc_w1), FOVR: a frame arrived with the
FIFO full (rc_w1): MCR.RFLM = 0 overwrites the last message, RFLM = 1 discards the new one.  RFOMx releases the output
mailbox (RIxR/RDTxR/RDLxR/RDHxR show the oldest message; RDTxR.FMI = filter match index).  The bxCAN does not
receive its own frames except in loop back mode.  Reception is off while FMR.FINIT = 1.

Filters (§32.7.4): 28 banks in CAN1's register space, shared: banks 0 .. CAN2SB - 1 belong to CAN1, CAN2SB .. 27 to
CAN2 (CAN2SB = 0: none for CAN1; 28: all for CAN1).  FM1R (1 = identifier list), FS1R (1 = single 32-bit scale),
FFA1R (FIFO), FA1R (active).  32-bit: STID[31:21] EXID[20:3] IDE RTR; mask mode FR1 = id, FR2 = mask (1 = compare);
list mode FR1, FR2 = two ids.  16-bit: STID[15:5] RTR IDE EXID[17:15]; mask mode FR1 = mask1:id1 (high:low), FR2 =
mask2:id2; list mode four ids (FR1 low, FR1 high, FR2 low, FR2 high).  Filter numbers (FMI) count per FIFO through
the controller's banks in order (active or not; 32-bit mask 1, 32-bit list 2, 16-bit mask 2, 16-bit list 4), from
the controller's first bank (CAN2: from CAN2SB).  Priority (Figure 343): 32-bit before 16-bit, list before mask, then
the lower filter number.  FM1R/FS1R/FFA1R and FMR.CAN2SB are writable only with FINIT = 1, FiRx only with FINIT = 1
or the bank inactive (other writes are ignored with one `warn`).  CAN2's own filter address range (offset >= 0x200)
is not implemented on silicon: reads 0, writes ignored (one `warn`).

Errors (§32.7.6, chipem.canbus.CanErrorCounters): ESR.TEC/REC, EWGF (a counter >= 96), EPVF (> 127), BOFF, LEC
(0 = transfer OK; 2 form error = bit-rate mismatch on reception, 3 ACK error, 5 bit dominant error; software may write
it, e.g. 7).  Bus off: no bus activity; MCR.ABOM = 1 recovers automatically after 128 x 11 recessive bits, ABOM = 0
after software enters and leaves Initialization mode (128 x 11 bits from leaving it).
IRQs (by name from the chip YAML, level lines): "<name>_TX" = TMEIE & any RQCPx; "<name>_RX0" = FMPIE0 & FMP0 != 0 |
FFIE0 & FULL0 | FOVIE0 & FOVR0; "<name>_RX1" likewise; "<name>_SCE" = ERRIE & ERRI | WKUIE & WKUI | SLKIE & SLAKI,
where MSR.ERRI is set when an error condition occurs whose enable is set (EWGIE, EPVIE, BOFIE, LECIE with LEC != 0).
Pins: CANx_RX / CANx_TX from the family signal table (pin muxed to the function, AF9); TX and RX are needed to
transmit (else bit errors), RX to receive and to synchronise.  Clock: RCC_APB1ENR.CANxEN (an access with the clock
off logs one `warn`; the node is off the bus), RCC_APB1RSTR.CANxRST resets the model (CAN1: the filters too).

Not modelled: resynchronisation / SJW / sample point, MSR.SAMP/RX (read 1 = recessive, the 0x0C02 reset value),
stuff/CRC/form errors of corrupted frames (frames on the emulated bus are never corrupted), overload frames, DBF, the
wake-up SOF timing (RM0090 32.9.2: WKUI and the AWUM wake-up happen at the SOF; here a sleeping node notices bus
activity at the ACK slot of the frame, chipem/canbus.py reports nothing earlier).  FULL and FOVR are cleared by
software only (rc_w1, RM0090 32.9.2 CAN_RFxR; releasing a message does not clear FULL).  The RM does not state
the FMI numbering for CAN2: it restarts at CAN2SB here (ST's HAL makes the same assumption).
"""
from __future__ import annotations

import re
from collections import deque
from typing import Any

from ...canbus import BUS_IDLE_BITS, BUSOFF_RECOVERY_BITS, INTERMISSION, CanBus, CanErrorCounters, CanFrame, pin_reason
from ..base import MMIOBlock
from ..registry import register
from ._util import GatedBlock, apb_hz, build_regs, irq_number

# MCR
INRQ, SLEEP, TXFP, RFLM, NART, AWUM, ABOM, TTCM, RESET, DBF = (1, 2, 4, 8, 16, 32, 64, 128, 1 << 15, 1 << 16)
# MSR
INAK, SLAK, ERRI, WKUI, SLAKI, TXM, RXM, SAMP, RX = 1, 2, 4, 8, 16, 1 << 8, 1 << 9, 1 << 10, 1 << 11
# TSR (per mailbox k: bits << 8k)
RQCP, TXOK, ALST, TERR, ABRQ = 1, 2, 4, 8, 0x80
# RFxR
FULL, FOVR, RFOM = 8, 16, 32
# IER
TMEIE, FMPIE0, FFIE0, FOVIE0, FMPIE1, FFIE1, FOVIE1 = 1, 2, 4, 8, 16, 32, 64
EWGIE, EPVIE, BOFIE, LECIE, ERRIE, WKUIE, SLKIE = 1 << 8, 1 << 9, 1 << 10, 1 << 11, 1 << 15, 1 << 16, 1 << 17
# BTR
LBKM, SILM = 1 << 30, 1 << 31
# FMR
FINIT = 1
TIR = 0x180
RIR = 0x1B0
FILT = 0x240
N_BANKS = 28
LEC_FORM, LEC_ACK, LEC_BIT_DOM = 2, 3, 5


@register("stm32.can")
class BxCAN(GatedBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        m = re.search(r"(\d+)$", name)
        self.index = int(params.get("index", m.group(1) if m else 1))
        self.gate = params.get("gate") or f"CAN{self.index}"
        self.has_filters = bool(params.get("filters", self.index == 1))
        self.n_banks = int(params.get("filter_banks", N_BANKS))
        self.errors = CanErrorCounters()
        self._fifo = (deque(), deque())
        self._order: dict[int, int] = {}          # pending mailbox -> request sequence number (TXFP = 1)
        self._seq = 0
        self.mode = "sleep"
        self._want_normal = False
        self._sync_ev = self._boff_ev = self._mode_ev = None
        self._t0 = 0.0                             # CAN timer base (TTCM)
        self._requesting = False
        self._req_t = 0.0
        self._wake_t = 0.0
        self._wval: tuple[int, int, int] = (0, 4, 0)
        self._lines: dict[str, bool] = {}
        self._irqs: dict[str, int | None] = {}
        self._pins: dict = {}
        self._warned: set[str] = set()
        self._master: BxCAN | None = None
        self.tx_frames = self.rx_frames = self.rx_lost = 0
        ov: dict[str, dict] = {
            "MCR": dict(on_write=self._mcr_write),
            "MSR": dict(ro_mask=0xFFFFFFFF, on_read=self._msr_read, on_write=self._msr_write),
            "TSR": dict(ro_mask=0xFFFFFFFF, on_read=self._tsr_read, on_write=self._tsr_write),
            "RF0R": dict(ro_mask=0xFFFFFFFF, on_read=self._rfr_reader(0), on_write=self._rfr_writer(0)),
            "RF1R": dict(ro_mask=0xFFFFFFFF, on_read=self._rfr_reader(1), on_write=self._rfr_writer(1)),
            "IER": dict(on_write=self._ier_write),
            "ESR": dict(ro_mask=0xFFFFFF8F, on_read=self._esr_read, on_write=self._esr_write),
            "BTR": dict(on_write=self._btr_write, reset=0x01230000),     # RM0090 32.9.2 (the SVD says 0)
            "FMR": dict(on_write=self._fmr_write),
            "FM1R": dict(on_write=self._finit_guard),
            "FS1R": dict(on_write=self._finit_guard),
            "FFA1R": dict(on_write=self._finit_guard),
        }
        for k in range(3):
            for reg in (f"TI{k}R", f"TDT{k}R", f"TDL{k}R", f"TDH{k}R"):
                ov[reg] = dict(on_write=self._mb_writer(k))
        for b in range(N_BANKS):
            for j in (1, 2):
                ov[f"F{b}R{j}"] = dict(on_write=self._bank_writer(b))
        r = build_regs((name, f"CAN{self.index}", "CAN1"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
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
        for key in ("TX", "RX0", "RX1", "SCE"):
            self._irqs[key] = irq_number(chip, f"{self.name}_{key}") or irq_number(chip, f"CAN{self.index}_{key}")

    def master(self) -> "BxCAN | None":
        """The instance holding the filter banks (CAN1): this one or the first stm32.can that has them."""
        if self.has_filters:
            return self
        m = self._master
        if m is None and self.chip is not None:
            for b in self.chip.peripherals.values():
                if isinstance(b, BxCAN) and b.has_filters:
                    m = self._master = b
                    break
        return m

    def reset(self) -> None:
        self._cancel_events()
        if self.chip is not None:
            self.can_bus.node_reset(self)
            self._loop_bus.node_reset(self)
        for key, lvl in list(self._lines.items()):
            if lvl:
                self._set_line(key, False)
        super().reset()
        self._soft_state()

    def _cancel_events(self) -> None:
        if self.chip is not None:
            for ev in (self._sync_ev, self._boff_ev, self._mode_ev):
                self.chip.sched.cancel(ev)
        self._sync_ev = self._boff_ev = self._mode_ev = None

    def _soft_state(self) -> None:
        self.errors.reset()
        for f in self._fifo:
            f.clear()
        self._order.clear()
        self.mode = "sleep"
        self._want_normal = False
        self._requesting = False

    def _master_reset(self) -> None:
        """MCR.RESET: everything but the filter banks back to reset values, Sleep mode."""
        keep = bytes(self.mem[0x200:])
        self.reset()
        self.mem[0x200:] = keep

    # ------------------------------------------------------------------ access
    def write(self, offset: int, size: int, value: int) -> None:
        if self._gate_loc is not None:
            self._gate_ok()                          # one `warn` when accessed with the clock off
        if offset >= 0x200 and not self.has_filters:
            self._warn_once("slavefilt", f"{self.name}: filter registers exist only in CAN1 (CAN2 shares CAN1's "
                                         f"filter banks, CAN2SB): write to +0x{offset:x} ignored")
            return
        self._wval = (offset, size, value)
        MMIOBlock.write(self, offset, size, value)

    def read(self, offset: int, size: int) -> int:
        if offset >= 0x200 and not self.has_filters:
            self._warn_once("slavefilt_r", f"{self.name}: filter registers exist only in CAN1: read of +0x{offset:x} "
                                           "returns 0")
            return 0
        return MMIOBlock.read(self, offset, size)

    def _written(self, reg) -> int:
        """Bits the CPU wrote to `reg` in the current write (raw, before masks; partial writes in their lane)."""
        off, size, value = self._wval
        sh = (off - reg.offset) * 8
        return (value << sh) & (((1 << (8 * size)) - 1) << sh) & 0xFFFFFFFF

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=msg)

    # ------------------------------------------------------------------ clocks / pins
    def _gate_on(self) -> bool:
        loc = self._gate_loc
        if loc is not None and not self._rcc.mem[loc[0]] & loc[1]:
            return False
        if not self.has_filters:
            m = self.master()
            if m is not None and not m._gate_on():
                return False
        return True

    def clock_hz(self) -> float:
        return apb_hz(self.chip, 1) if self.chip is not None else 0.0

    def can_bit_ns(self) -> float:
        btr = self.get("BTR")
        hz = self.clock_hz()
        if hz <= 0:
            return 0.0
        tq = ((btr & 0x3FF) + 1) / hz
        return 1e9 * tq * (1 + ((btr >> 16 & 0xF) + 1) + ((btr >> 20 & 7) + 1))

    def bitrate(self) -> float:
        b = self.can_bit_ns()
        return 1e9 / b if b > 0 else 0.0

    def _rx_pin_ok(self) -> bool:
        return pin_reason(self.chip, self.rx_signal, self._pins) is None

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
        return self._loop_bus if self.get("BTR") & LBKM else self.can_bus

    def _on_bus(self, bus: CanBus) -> bool:
        return (self.chip is not None and bus is self._bus() and self.mode == "normal" and not self.errors.busoff
                and self._gate_on())

    def _tx_allowed(self, bus: CanBus) -> bool:
        """May start a frame: on the bus, not silent, no Initialization / Sleep request waiting."""
        return (self._on_bus(bus) and not (self.get("BTR") & SILM and bus is not self._loop_bus)
                and self._target() == "normal")

    lp_frozen: Any = False              # PWR freezer: APB1 clock off in Stop / gated Sleep

    def time_shift(self, dns: float, dcycles: int) -> None:
        """Clock back after Stop: the TTCM timer held, the pending bus-off recovery / mode events were held."""
        self._t0 += dns
        if self.chip is not None and self._requesting:
            self._bus().request(self)

    def can_tx_frame(self, bus: CanBus) -> CanFrame | None:
        if self.lp_frozen or not self._tx_allowed(bus):
            return None
        k = self._best_tx()
        return None if k is None else self._frame_of(k)

    def can_tx_connected(self, bus: CanBus) -> bool:
        if bus is self._loop_bus:
            return True
        return pin_reason(self.chip, self.tx_signal, self._pins) is None and self._rx_pin_ok()

    def can_rx_state(self, bus: CanBus) -> str | None:
        if self.lp_frozen:
            return None                 # no clock in Stop: neither receives nor acknowledges (wake-up: EXTI on CANx_RX)
        if self.mode == "sleep" and bus is self.can_bus and self.chip is not None:
            self._bus_activity_asleep()
        if not self._on_bus(bus):
            return None
        if bus is self._loop_bus:
            return "active"
        if not self._rx_pin_ok():
            return None
        return "listen" if self.get("BTR") & SILM else "active"

    def can_error_passive(self) -> bool:
        return self.errors.state == "passive"

    def can_rx(self, bus: CanBus, frame: CanFrame, t_sof: float, t: float) -> None:
        if self.can_rx_state(bus) is None:
            return
        if self._requesting and self._req_t < t_sof + self.can_bit_ns() and bus is not self._loop_bus:
            k = self._best_tx()                   # we competed for this SOF and lost the arbitration
            if k is not None:
                if self.get("MCR") & NART:
                    self._mb_complete(k, ok=False, flags=ALST)
                    self._tx_changed()
                else:
                    self._tsr_bits(k, ALST, ALST)
        if not self.get("BTR") & SILM:
            self._err_events(self.errors.rx_ok())
        self._set_lec(0)
        self.rx_frames += 1
        self._receive(frame, t_sof)

    def can_rx_error(self, bus: CanBus, kind: str, t: float) -> None:
        if self.can_rx_state(bus) is None or kind != "form":
            return
        self._set_lec(LEC_FORM)
        if not self.get("BTR") & SILM:
            self._err_events(self.errors.rx_error())

    def can_tx_done(self, bus: CanBus, frame: CanFrame, ok: bool, err: str | None, t_sof: float, t: float) -> None:
        k = frame.tag
        pending = k is not None and bool(self.get(f"TI{k}R") & 1)
        if ok:
            self.tx_frames += 1
            self._err_events(self.errors.tx_ok())
            self._set_lec(0)
            if bus is self._loop_bus:
                lost = self.rx_lost
                self._receive(frame, t_sof)          # loop back: our own frame through our filters
                self.rx_lost = lost
            if pending:
                if self.get("MCR") & TTCM:
                    tdt = self.get(f"TDT{k}R")
                    self.set(f"TDT{k}R", (tdt & 0xFFFF) | (self._timer(t_sof) << 16))
                self._mb_complete(k, ok=True)
        else:
            self._set_lec(LEC_ACK if err == "ack" else LEC_BIT_DOM)
            self._err_events(self.errors.tx_error(err or "bit"))
            if pending:
                if self.get("MCR") & NART or self.get("TSR") & (ABRQ << (8 * k)):
                    self._mb_complete(k, ok=False, flags=TERR)
                else:
                    self._tsr_bits(k, TERR | TXOK, TERR)
        self._requesting = False
        self._tx_changed()

    # ------------------------------------------------------------------ modes
    def _mcr_write(self, blk, reg, old, new):
        if new & RESET:
            self._master_reset()
            self.emit("info", block=self.name, msg=f"{self.name}: MCR.RESET (master reset, sleep mode)")
            return self.get("MCR")
        self.set("MCR", new)
        if (old ^ new) & TTCM:
            self._t0 = self.now_ns()
        self._update_mode()
        if (old ^ new) & TXFP:
            self._tx_changed()
        return None

    def _target(self) -> str:
        mcr = self.get("MCR")
        return "init" if mcr & INRQ else "sleep" if mcr & SLEEP else "normal"

    def _update_mode(self) -> None:
        if self.chip is None:
            return
        want = self._target()
        if want == "normal":
            if self.mode != "normal" and not self._want_normal:
                self._want_normal = True
                self._try_sync()
            return
        self._want_normal = False
        self.chip.sched.cancel(self._sync_ev)
        self._sync_ev = None
        if self.mode == want:
            return
        bus = self._bus()
        bus.withdraw(self)
        self._requesting = False
        if self.mode == "normal" and bus.activity(self, self.now_ns()) is not None:
            if self._mode_ev is None:                 # granted at the end of the current bus activity
                self._mode_ev = self.chip.sched.in_ns(max(self.can_bit_ns(), 1.0), self._mode_poll)
            return
        self._enter(want)

    def _mode_poll(self) -> None:
        self._mode_ev = None
        want = self._target()
        if want == "normal" or want == self.mode:
            return
        if self.mode == "normal" and self._bus().activity(self, self.now_ns()) is not None:
            self._mode_ev = self.chip.sched.in_ns(max(self.can_bit_ns(), 1.0), self._mode_poll)
            return
        self._enter(want)

    def _enter(self, mode: str) -> None:
        prev = self.mode
        self.mode = mode
        self._bus().withdraw(self)
        self._requesting = False
        if mode == "sleep" and prev != "sleep" and self.get("IER") & SLKIE:
            self._msr_flag(SLAKI)
        self._update_irq()

    def _try_sync(self) -> None:
        """Leaving Initialization / Sleep mode: wait for 11 recessive bits on CANRX (bus idle)."""
        if not self._want_normal or self._sync_ev is not None or self.chip is None:
            return
        if not self._gate_on():
            self._warn_once("sync_gate", f"{self.name}: cannot leave {self.mode} mode: clock off (RCC_APB1ENR."
                                         f"CAN{self.index}EN" + ("" if self.has_filters else " / CAN1EN: CAN2 is "
                                                                 "clocked through CAN1") + ")")
            return
        loop = bool(self.get("BTR") & LBKM)
        if not loop and not self._rx_pin_ok():
            self._warn_once("sync_pin", f"{self.name}: cannot leave {self.mode} mode: {self.rx_signal} is not muxed "
                                        "to a pin, the bxCAN never sees 11 recessive bits (INAK/SLAK stay set)")
            return
        bit = self.can_bit_ns()
        if bit <= 0:
            return
        now = self.now_ns()
        idle = now if loop else max(now, self.can_bus.idle_ns, self._wake_t)
        self._sync_ev = self.chip.sched.at_ns(idle + BUS_IDLE_BITS * bit, self._synced)

    def _synced(self) -> None:
        self._sync_ev = None
        if not self._want_normal:
            return
        self._want_normal = False
        prev = self.mode
        self.mode = "normal"
        self._t0 = self.now_ns()
        if prev == "sleep":
            self._msr_flag(SLAKI, False)               # cleared by hardware with SLAK
        if self.errors.busoff and not self.get("MCR") & ABOM and prev == "init":
            self._sched_recovery(self.now_ns() + (BUSOFF_RECOVERY_BITS - BUS_IDLE_BITS) * self.can_bit_ns())
        self._update_irq()
        self._tx_changed()

    def _bus_activity_asleep(self) -> None:
        if not self._gate_on() or not self._rx_pin_ok() or self.get("BTR") & LBKM:
            return
        self._msr_flag(WKUI)
        # seen at the ACK slot: the frame still has ACK delimiter + EOF (8 bits) and the intermission to go
        self._wake_t = self.now_ns() + (8 + INTERMISSION) * self.can_bit_ns()
        mcr = self.get("MCR")
        if mcr & AWUM and mcr & SLEEP and not mcr & INRQ:
            self.set("MCR", mcr & ~SLEEP)              # automatic wake-up: SLEEP cleared by hardware
            self.emit("info", block=self.name, msg=f"{self.name}: wake-up on bus activity (AWUM)")
            self._update_mode()

    # ------------------------------------------------------------------ status registers
    def _msr_read(self, blk, reg):
        if self._want_normal and self._sync_ev is None:
            self._try_sync()                           # a pin / clock configured since: retry
        v = self.get("MSR") & (ERRI | WKUI | SLAKI)
        mcr = self.get("MCR")
        if self.mode == "init":
            v |= INAK
            if mcr & SLEEP:
                v |= SLAK
        elif self.mode == "sleep":
            v |= SLAK
        if self.chip is not None and self.mode == "normal":
            act = self._bus().activity(self, self.now_ns())
            v |= TXM if act == "tx" else RXM if act == "rx" else 0
        return v | SAMP | RX

    def _msr_write(self, blk, reg, old, new):
        w = self._written(reg) & (ERRI | WKUI | SLAKI)
        v = old & ~w
        self.set("MSR", v)
        self._update_irq()
        return v

    def _msr_flag(self, bit: int, on: bool = True) -> None:
        v = self.get("MSR")
        self.set("MSR", (v | bit) if on else (v & ~bit))
        self._update_irq()

    def _pending(self, k: int) -> bool:
        return bool(self.get(f"TI{k}R") & 1)

    def _prio_list(self) -> list[int]:
        """Pending mailboxes, highest priority first."""
        pend = [k for k in range(3) if self._pending(k)]
        if self.get("MCR") & TXFP:
            return sorted(pend, key=lambda k: self._order.get(k, 0))
        return sorted(pend, key=lambda k: (self._frame_of(k).arb, k))

    def _tsr_read(self, blk, reg):
        v = self.get("TSR") & 0x008F8F8F
        prio = self._prio_list()
        empty = [k for k in range(3) if k not in prio]
        for k in empty:
            v |= 1 << (26 + k)
        if empty:
            code = empty[0]
        else:
            code = prio[-1]
        if len(prio) > 1:
            v |= 1 << (29 + prio[-1])
        return v | (code << 24)

    def _tsr_bits(self, k: int, mask: int, value: int) -> None:
        sh = 8 * k
        v = self.get("TSR")
        self.set("TSR", (v & ~(mask << sh)) | ((value & mask) << sh))

    def _tsr_write(self, blk, reg, old, new):
        w = self._written(reg)
        v = old
        for k in range(3):
            if w >> (8 * k) & RQCP:
                v &= ~((RQCP | TXOK | ALST | TERR) << (8 * k))
        self.set("TSR", v)
        for k in range(3):
            if w >> (8 * k) & ABRQ and self._pending(k):
                self._abort(k)
        self._update_irq()
        return self.get("TSR")

    def _abort(self, k: int) -> None:
        fr = self._bus().transmitting(self) if self.chip is not None else None
        if fr is not None and fr.tag == k:
            self._tsr_bits(k, ABRQ, ABRQ)           # on the bus: decided by the outcome of this attempt
            return
        self._mb_complete(k, ok=False)
        self._tx_changed()

    def _mb_complete(self, k: int, ok: bool, flags: int = 0) -> None:
        self._tsr_bits(k, RQCP | TXOK | ALST | TERR | ABRQ, RQCP | (TXOK if ok else 0) | flags)
        self.set(f"TI{k}R", self.get(f"TI{k}R") & ~1)
        self._order.pop(k, None)
        self._update_irq()

    def _rfr_reader(self, f: int):
        def hook(blk, reg):
            return (self.get(reg.name) & (FULL | FOVR)) | len(self._fifo[f])
        return hook

    def _rfr_writer(self, f: int):
        def hook(blk, reg, old, new):
            w = self._written(reg)
            v = old & ~(w & (FULL | FOVR)) & (FULL | FOVR)
            self.set(reg.name, v)
            if w & RFOM and self._fifo[f]:
                self._fifo[f].popleft()
                self._fifo_output(f)
            self._update_irq()
            return v
        return hook

    def _esr_read(self, blk, reg):
        e = self.errors
        v = self.get("ESR") & 0x70
        if e.tec >= 96 or e.rec >= 96:
            v |= 1
        if e.tec > 127 or e.rec > 127:
            v |= 2
        if e.busoff:
            v |= 4
        return v | ((e.tec & 0xFF) << 16) | ((e.rec & 0xFF) << 24)

    def _esr_write(self, blk, reg, old, new):
        return new & 0x70

    def _set_lec(self, code: int) -> None:
        self.set("ESR", (self.get("ESR") & ~0x70) | (code << 4))
        if code and self.get("IER") & LECIE:
            self._msr_flag(ERRI)

    def _ier_write(self, blk, reg, old, new):
        self.set("IER", new)
        self._update_irq()
        return None

    def _btr_write(self, blk, reg, old, new):
        if self.mode != "init":
            self._warn_once("btr", f"{self.name}: BTR written outside initialization mode (INAK = 0): ignored")
            return old
        return None

    # ------------------------------------------------------------------ errors
    def _err_events(self, evs: set[str]) -> None:
        if not evs:
            return
        ier = self.get("IER")
        if ("tx_warn" in evs or "rx_warn" in evs) and ier & EWGIE or "passive" in evs and ier & EPVIE \
                or "busoff" in evs and ier & BOFIE:
            self._msr_flag(ERRI)
        if "busoff" in evs:
            self.emit("info", block=self.name, msg=f"{self.name}: bus off (TEC > 255)")
            if self.chip is not None:
                self._bus().withdraw(self)
                self._requesting = False
                if self.get("MCR") & ABOM:
                    self._sched_recovery(self.now_ns() + BUSOFF_RECOVERY_BITS * self.can_bit_ns())
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
        if self.chip is None:
            return
        ier, tsr, msr = self.get("IER"), self.get("TSR"), self.get("MSR")
        self._set_line("TX", bool(ier & TMEIE and tsr & 0x010101))
        for f, (fmp, ff, fov) in enumerate(((FMPIE0, FFIE0, FOVIE0), (FMPIE1, FFIE1, FOVIE1))):
            rf = self.get(f"RF{f}R")
            self._set_line(f"RX{f}", bool(ier & fmp and self._fifo[f] or ier & ff and rf & FULL
                                          or ier & fov and rf & FOVR))
        self._set_line("SCE", bool(ier & ERRIE and msr & ERRI or ier & WKUIE and msr & WKUI
                                   or ier & SLKIE and msr & SLAKI))

    # ------------------------------------------------------------------ transmit mailboxes
    def _mb_writer(self, k: int):
        ti = f"TI{k}R"

        def hook(blk, reg, old, new):
            if reg.name != ti:
                if self._pending(k):
                    self._warn_once(f"mbwp{k}", f"{self.name}: write to {reg.name} while mailbox {k} is pending "
                                                "(write-protected): ignored")
                    return old
                return None
            if old & 1:                                 # pending: write-protected (TXRQ stays set)
                self._warn_once(f"mbwp{k}", f"{self.name}: write to {ti} while mailbox {k} is pending "
                                            "(write-protected): ignored")
                return old
            if new & 1:
                self._seq += 1
                self._order[k] = self._seq
                self.set(ti, new)
                # RM0090 32.9.2 CAN_TSR: RQCPx (and with it TXOKx/ALSTx/TERRx) cleared by hardware on TXRQ
                self._tsr_bits(k, RQCP | TXOK | ALST | TERR, 0)
                self._update_irq()
                self._tx_changed()
            return None
        return hook

    def _frame_of(self, k: int) -> CanFrame:
        ti, tdt = self.get(f"TI{k}R"), self.get(f"TDT{k}R")
        ext = bool(ti & 4)
        ident = (ti >> 3) & 0x1FFFFFFF if ext else (ti >> 21) & 0x7FF
        data = self.get(f"TDL{k}R").to_bytes(4, "little") + self.get(f"TDH{k}R").to_bytes(4, "little")
        dlc = tdt & 0xF
        if tdt & (1 << 8) and dlc == 8 and self.get("MCR") & TTCM:
            tm = self._timer(self.now_ns())             # TGT: time stamp in data bytes 6-7
            data = data[:6] + bytes((tm & 0xFF, tm >> 8))
        return CanFrame(ident, data, ext=ext, rtr=bool(ti & 2), dlc=dlc, tag=k)

    def _best_tx(self) -> int | None:
        tsr = self.get("TSR")
        for k in self._prio_list():
            if not tsr >> (8 * k) & ABRQ:
                return k
        return None

    def _tx_changed(self) -> None:
        if self.chip is None:
            return
        bus = self._bus()
        if self._tx_allowed(bus) and self._best_tx() is not None:
            if not self._requesting:
                self._requesting = True
                self._req_t = self.now_ns()
            bus.request(self)
        else:
            self._requesting = False
            bus.withdraw(self)

    def _timer(self, t: float) -> int:
        if not self.get("MCR") & TTCM:
            return 0
        bit = self.can_bit_ns()
        return int(max(0.0, t - self._t0) // bit) & 0xFFFF if bit > 0 else 0

    # ------------------------------------------------------------------ reception
    def _receive(self, fr: CanFrame, t_sof: float) -> None:
        m = self.master()
        if m is None:
            self.rx_lost += 1
            return
        if m.get("FMR") & FINIT:
            self.rx_lost += 1                       # filter initialization mode: reception deactivated
            return
        hit = m.filter_match(fr, slave=m is not self)
        if hit is None:
            self.rx_lost += 1
            return
        f, fmi = hit
        q = self._fifo[f]
        rf = self.get(f"RF{f}R")
        item = self._mailbox_words(fr, fmi, t_sof)
        if len(q) >= 3:
            rf |= FOVR
            self.rx_lost += 1
            if not self.get("MCR") & RFLM:
                q[-1] = item                         # FIFO not locked: the last message is overwritten
        else:
            q.append(item)
            if len(q) == 3:
                rf |= FULL
            if len(q) == 1:
                self._fifo_output(f)
        self.set(f"RF{f}R", rf & (FULL | FOVR))
        self._update_irq()

    def _mailbox_words(self, fr: CanFrame, fmi: int, t_sof: float) -> tuple[int, int, int, int]:
        if fr.ext:
            ri = (fr.id << 3) | 4
        else:
            ri = fr.id << 21
        ri |= int(fr.rtr) << 1
        data = fr.data + bytes(8 - len(fr.data))
        rdt = fr.dlc | ((fmi & 0xFF) << 8) | (self._timer(t_sof) << 16)
        return ri, rdt, int.from_bytes(data[:4], "little"), int.from_bytes(data[4:], "little")

    def _fifo_output(self, f: int) -> None:
        q = self._fifo[f]
        if not q:
            return
        ri, rdt, lo, hi = q[0]
        o = RIR + 0x10 * f
        for i, v in enumerate((ri, rdt, lo, hi)):
            self.mem[o + 4 * i:o + 4 * i + 4] = v.to_bytes(4, "little")

    # ------------------------------------------------------------------ filters (CAN1 only)
    def _fmr_write(self, blk, reg, old, new):
        if not (old | new) & FINIT and (old ^ new) & 0x3F00:
            self._warn_once("can2sb", f"{self.name}: FMR.CAN2SB written with FINIT = 0: ignored")
            new = (new & ~0x3F00) | (old & 0x3F00)
        return new

    def _finit_guard(self, blk, reg, old, new):
        if not self.get("FMR") & FINIT and old != new:
            self._warn_once(f"fg{reg.name}", f"{self.name}: {reg.name} written outside filter initialization mode "
                                             "(FMR.FINIT = 0): ignored")
            return old
        return None

    def _bank_writer(self, b: int):
        def hook(blk, reg, old, new):
            if not self.get("FMR") & FINIT and self.get("FA1R") >> b & 1 and old != new:
                self._warn_once(f"fb{b}", f"{self.name}: filter bank {b} written while active and FMR.FINIT = 0: "
                                          "ignored")
                return old
            return None
        return hook

    def bank_range(self, slave: bool) -> range:
        sb = min(self.get("FMR") >> 8 & 0x3F, self.n_banks)
        return range(sb, self.n_banks) if slave else range(0, sb)

    def filter_match(self, fr: CanFrame, slave: bool = False) -> tuple[int, int] | None:
        """(FIFO, filter match index) of the highest-priority filter of CAN1 (slave=False) or CAN2 accepting
        `fr`, None if no active filter accepts it."""
        fm, fs, ffa, fa = self.get("FM1R"), self.get("FS1R"), self.get("FFA1R"), self.get("FA1R")
        stid, exid = (fr.id >> 18, fr.id & 0x3FFFF) if fr.ext else (fr.id, 0)
        w32 = (stid << 21) | (exid << 3) | (int(fr.ext) << 2) | (int(fr.rtr) << 1)
        w16 = (stid << 5) | (int(fr.rtr) << 4) | (int(fr.ext) << 3) | (exid >> 15 & 7)
        num = [0, 0]
        best = None
        for b in self.bank_range(slave):
            lst, s32, fifo = fm >> b & 1, fs >> b & 1, ffa >> b & 1
            n0 = num[fifo]
            num[fifo] += (2 if lst else 1) if s32 else (4 if lst else 2)
            if not fa >> b & 1:
                continue
            r1 = int.from_bytes(self.mem[FILT + 8 * b:FILT + 8 * b + 4], "little")
            r2 = int.from_bytes(self.mem[FILT + 8 * b + 4:FILT + 8 * b + 8], "little")
            hits: list[int] = []
            if s32:
                if lst:
                    hits = [i for i, r in enumerate((r1, r2)) if not (w32 ^ r) & ~1 & 0xFFFFFFFF]
                elif not (w32 ^ r1) & r2 & ~1:
                    hits = [0]
            elif lst:
                hits = [i for i, r in enumerate((r1 & 0xFFFF, r1 >> 16, r2 & 0xFFFF, r2 >> 16)) if r == w16]
            else:
                hits = [i for i, r in enumerate((r1, r2)) if not (w16 ^ (r & 0xFFFF)) & (r >> 16)]
            if hits:
                key = (0 if s32 else 1, 0 if lst else 1, b, hits[0])
                if best is None or key < best[0]:
                    best = (key, fifo, n0 + hits[0])
        return None if best is None else (best[1], best[2])

    # ------------------------------------------------------------------ reporting
    def can_stats(self) -> dict:
        st = self.mode if self.mode != "normal" else self.errors.state
        return {"bitrate": self.bitrate(), "tx": self.tx_frames, "rx": self.rx_frames, "lost": self.rx_lost,
                "tec": self.errors.tec & 0xFF, "rec": self.errors.rec, "state": st,
                "bus": self.can_bus.name if self.can_bus is not self._own_bus else None}

