"""TI C28x eCAN (F2833x/F2823x eCAN-A 0x6000, eCAN-B 0x6200): 32 mailboxes on a chipem.canbus bus.

Source: SPRUI07 (TMS320x2833x/2823x TRM, March 2020) ch. 13 "Enhanced Controller Area Network (eCAN)" (SPRUEU1A now
points there): §13.4.2 memory map (control registers 0x00-0x33, LAM 0x40, MOTS 0x80, MOTO 0xC0, mailboxes at
+0x100, 8 words each: MSGID, MSGCTRL, MDL, MDH; eCAN-B mailboxes at 0x6300), §13.5-13.6 mailboxes (Table 13-1 object
types, transmit priority, receive scan from the highest mailbox, OPC/RML), §13.7.1 initialization (CCR/CCE
handshake synchronised to 11 recessive bits, CANBTC with TSEG1/TSEG2 = 0 never leaves init), §13.7.1.2 bit rate =
SYSCLKOUT / 2 / (BRP x (TSEG1 + TSEG2 + 1)), §13.7.1.4 EALLOW protection, §13.7.2-13.7.3 transmit / receive / remote
frames / CDR, §13.7.4 interrupts (Table 13-8), §13.7.5 local power-down, §13.8 registers (Figures 13-10 .. 13-44),
§13.8.18 time stamps and time-outs, §13.9 DBO byte order, §13.10 acceptance filter (LAM/GAM, 1 = don't care, LAMI);
SPRUI07 Table 1-97 (EALLOW-protected eCAN registers); PCLKCR0 bits 14/15 (ECANAENCLK/ECANBENCLK); PIE INT9.5/9.6
(eCAN-A ECAN0INTA/ECAN1INTA), 9.7/9.8 (eCAN-B).  Register table: chips/tms320f28232_regs.yaml (hand-written).

Modes: after reset CANMC.CCR = CANES.CCE = 1 (initialization: off the bus, CANBTC writable).  Clearing CCR with a
valid CANBTC (TSEG1reg, TSEG2reg != 0) clears CCE after 11 bit times (bus idle detection), clears the error counters
and puts the node on the bus; setting CCR sets CCE at once (a frame of ours on the wire still completes).
CANMC.SCB: 0 = SCC mode (mailboxes 0-15, priority by mailbox number, GAM for 15..6, LAM3 for 5..3, LAM0 for 2..0,
no time stamps), 1 = eCAN mode (32 mailboxes, priority TPL then number, LAMn per mailbox, MOTS/MOTO/TSC).  SRES
resets the unprotected registers (not CANMC's protected bits, CANBTC, CANGIM, CANMIM, CANTSC, CANTIOC/RIOC, masks,
mailboxes or error counters) and cancels pending transmissions.  STM: self-test, frames go to a private bus that
acknowledges itself and are received by this node (stored in mailbox 0 if no receive mailbox is configured).  PDR:
local power-down at once (PDA = 1; reads return 0 except CANES = 0x08); PDR = 0 or, with WUBA, any bus frame (which is
lost) wakes it (WUIF), then 11 bit times of synchronisation.  ABO: automatic bus-on 128 x 11 bit times after bus off;
otherwise bus off sets CCR and the CPU clears it (recovery at the earliest 128 x 11 bit times after bus off).
EALLOW (Table 1-97): CANMC bits 15-9 and 7-6, CANBTC, CANGIM, CANMIM, CANTSC, CANTIOC, CANRIOC: writes without EALLOW
are ignored (one `warn`).  CANBTC and CANGAM (and LAM0/LAM3 in SCC mode) are writable only in init mode (CCE = 1).

Transmit: mailbox n is sent when TRS[n], ME[n] and not (CDR with MBNR = n); a transmit mailbox (MD = 0) sends a data
frame (or a remote frame with MSGCTRL.RTR), a receive mailbox (MD = 1) with RTR sends a remote request.  Highest
priority first (eCAN: TPL, then mailbox number; SCC: number).  Success: TA[n] (not for remote requests from receive
mailboxes), TRS/TRR cleared, MOTS[n] = TSC, TOS[n] cleared.  CANTRR[n]: a request not on the bus is aborted at once
(TRS = TRR = 0, AA = 1); one on the bus completes (TA) or, if it fails, is aborted (AA).  Writes are denied (WDIF) to
MSGID of an enabled mailbox, to MSGCTRL/MDL/MDH of an enabled receive mailbox and to MDL/MDH of a transmit mailbox
with TRS set unless CDR selects it.
Receive: a data frame is compared with the enabled receive mailboxes from the highest number down (eCAN 31, SCC 15):
ID bits under the acceptance mask (AME: LAMn / GAM / LAM0/3, a 1 bit is "don't care"; AME = 0 exact), IDE equal
unless LAMI/AMI.  A mailbox with RMP set is skipped if OPC[n], else overwritten with RML[n].  Stored: ID and IDE in
MSGID (AME/AAM kept; not in STM), DLC/RTR in MSGCTRL (TPL kept), data per DBO (0: byte 0 = MDL[31:24]; 1: byte 0 =
MDL[7:0]); RMP[n], MOTS[n], TOS[n] cleared.  MBCC: TSC cleared after mailbox 16 transmits/receives.  Remote frames:
the first matching mailbox from the top: a transmit mailbox with AAM gets RFP[n] and TRS[n] (auto answer), a receive
mailbox stores it like data; transmit mailboxes without AAM do not match.
Time stamps (eCAN mode): CANTSC counts bit times while out of init/power-down; time-outs set TOS[n] when TSC >= MOTOn
with TOC[n] (and TRS[n] for transmit mailboxes); clearing TOC[n] clears TOS[n].

Interrupts (§13.7.4): mailbox flags (TA|RMP) & MIM set GMIF on line MIL[n] (MIV = highest such mailbox), TOS sets
MTOF on line MIL[n]; AAIF (any AA), RMLIF (any RML), WLIF, EPIF, BOIF, WUIF, WDIF, TCOF on line GIL.  Line x =
IxEN & (GMIFx | MTOFx & MTOM | AAIFx & AAIM | RMLIFx & RMLIM | WLIFx & WLIM | EPIFx & EPIM | BOIFx & BOIM | WUIFx &
WUIM | WDIFx & WDIM | TCOFx & TCOM) -> PIE ECAN0INTx / ECAN1INTx; clearing a flag while others keep the line active
gives the PIE a new edge.  GMIF/MIV/AAIF/RMLIF/MTOF follow TA/RMP/AA/RML/TOS; WLIF..TCOF clear by writing 1.
Errors (chipem.canbus.CanErrorCounters): CANTEC/CANREC, CANES EW/EP/BO status, first-error flags ACKE (no ACK),
BE (TX/RX pin not muxed or CANTIOC/RIOC function bits clear: bit error), FE (bit-rate mismatch on reception); one set
error flag freezes the others until it is cleared by writing 1.  CANES.TM/RM from the bus.

The node is on the bus only with PCLKCR0 ECANxENCLK, CCE = 0, PDA = 0, not bus off, CANRIOC.RXFUNC and a pin muxed to
CANRXx (CANTIOC.TXFUNC and CANTXx too for transmitting).

Not modelled: SUSP / emulation suspend (SMA), TCOF (TSC MSB overflow after 2^31 bit times), the 80-cycle mailbox to
transmit-buffer copy delay, CRC/stuff errors (frames on the emulated bus are never corrupted), SA1 (cleared at the
first recessive bit seen once the node is on the bus), 16-bit-access corruption of the 32-bit-only registers (any
width works here).
"""
from __future__ import annotations

from typing import Any

from ...canbus import BUS_IDLE_BITS, BUSOFF_RECOVERY_BITS, CanBus, CanErrorCounters, CanFrame, pin_reason
from ..registry import register
from ._util import C2000Block, build_regs, irq_line

# CANMC
SUSP, MBCC, TCC, SCB, CCR, PDR, DBO, WUBA, CDR, ABO, STM, SRES = 1 << 16, 1 << 15, 1 << 14, 1 << 13, 1 << 12, \
    1 << 11, 1 << 10, 1 << 9, 1 << 8, 1 << 7, 1 << 6, 1 << 5
MC_PROTECTED = 0xFEC0
# CANES
TM, RM, PDA, CCE, SMA, EW, EP, BO, ACKE, SE, CRCE, SA1, BE, FE = 1, 2, 8, 16, 32, 1 << 16, 1 << 17, 1 << 18, \
    1 << 19, 1 << 20, 1 << 21, 1 << 22, 1 << 23, 1 << 24
ES_ERR = ACKE | SE | CRCE | BE | FE
# CANGIFx
WLIF, EPIF, BOIF, RMLIF, WUIF, WDIF, AAIF, GMIF, TCOF, MTOF = 1 << 8, 1 << 9, 1 << 10, 1 << 11, 1 << 12, 1 << 13, \
    1 << 14, 1 << 15, 1 << 16, 1 << 17
GIF_RC = WLIF | EPIF | BOIF | WUIF | WDIF | TCOF
# CANGIM
I0EN, I1EN, GIL = 1, 2, 4
WLIM, EPIM, BOIM, RMLIM, WUIM, WDIM, AAIM, TCOM, MTOM = 1 << 8, 1 << 9, 1 << 10, 1 << 11, 1 << 12, 1 << 13, 1 << 14, \
    1 << 16, 1 << 17
FUNC = 1 << 3                                  # CANTIOC.TXFUNC / CANRIOC.RXFUNC
M32 = 0xFFFFFFFF


@register("ti_c2000.ecan")
class ECAN(C2000Block):
    eallow_regs = ("CANBTC", "CANGIM", "CANMIM", "CANTSC", "CANTIOC", "CANRIOC")

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.letter = str(params.get("instance") or name[-1]).upper()
        self.errors = CanErrorCounters()
        self._cce = True
        self._pda = False
        self._sync_t = float("inf")
        self._evs: dict[str, Any] = {}
        self._tsc = (0, 0.0, False, 0.0)       # (value, base time, running, bit ns)
        self._lines = [False, False]
        self._lines_irq: list = [None, None]
        self._pins: dict = {}
        self._boff_t = 0.0
        self._warned_mc = False
        self.tx_frames = self.rx_frames = self.rx_lost = 0
        ov: dict[str, dict] = {
            "CANME": dict(on_write=self._tx_hook), "CANMD": dict(on_write=self._tx_hook),
            "CANTRS": dict(on_write=self._trs_write), "CANTRR": dict(on_write=self._trr_write),
            "CANTA": dict(w1c_mask=M32, on_write=self._clear_hook),
            "CANAA": dict(w1c_mask=M32, on_write=self._clear_hook),
            "CANRMP": dict(w1c_mask=M32, on_write=self._rmp_write),
            "CANRML": dict(ro_mask=M32),
            "CANRFP": dict(w1c_mask=M32),
            "CANGAM": dict(on_write=self._init_only),
            "CANMC": dict(on_write=self._mc_write),
            "CANBTC": dict(on_write=self._btc_write),
            "CANES": dict(ro_mask=M32 & ~ES_ERR, w1c_mask=ES_ERR, on_read=self._es_read, on_write=self._clear_hook),
            "CANTEC": dict(ro_mask=M32, on_read=lambda b, r: self.errors.tec & 0xFF),
            "CANREC": dict(ro_mask=M32, on_read=lambda b, r: self.errors.rec & 0xFF),
            "CANGIF0": dict(ro_mask=M32 & ~GIF_RC, w1c_mask=GIF_RC, on_read=self._gif_reader(0),
                            on_write=self._clear_hook),
            "CANGIF1": dict(ro_mask=M32 & ~GIF_RC, w1c_mask=GIF_RC, on_read=self._gif_reader(1),
                            on_write=self._clear_hook),
            "CANGIM": dict(on_write=self._irq_hook), "CANMIM": dict(on_write=self._irq_hook),
            "CANMIL": dict(on_write=self._irq_hook),
            "CANTSC": dict(on_read=self._tsc_read, on_write=self._tsc_write),
            "CANTOC": dict(on_write=self._toc_write),
            "CANTOS": dict(w1c_mask=M32, on_write=self._clear_hook),
        }
        for n in range(32):
            ov[f"MBOX{n}_MSGID"] = dict(on_write=self._mb_writer(n, "id"))
            ov[f"MBOX{n}_MSGCTRL"] = dict(on_write=self._mb_writer(n, "ctrl"))
            ov[f"MBOX{n}_MDL"] = dict(on_write=self._mb_writer(n, "data"))
            ov[f"MBOX{n}_MDH"] = dict(on_write=self._mb_writer(n, "data"))
            ov[f"MOTO{n}"] = dict(on_write=self._moto_write)
            if n in (0, 3):
                ov[f"LAM{n}"] = dict(on_write=self._lam_scc_write)
        regs = build_regs((name, f"ECAN{self.letter}", "ECANA"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, regs, params=params, log=log)
        self.gate = params.get("gate") or f"ECAN{self.letter}ENCLK"
        self.rx_signal = f"CANRX{self.letter}"
        self.tx_signal = f"CANTX{self.letter}"
        self._own_bus = CanBus(name)
        self._loop_bus = CanBus(f"{name}.selftest", self_ack=True)
        self.can_bus: CanBus = self._own_bus
        self._own_bus.attach(self)
        self._loop_bus.attach(self)

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        defaults = {"A": ("9.5", "9.6"), "B": ("9.7", "9.8")}.get(self.letter, (None, None))
        for i in (0, 1):
            line = self.params.get(f"irq{i}") or irq_line(chip, f"ECAN{i}INT{self.letter}") or \
                irq_line(chip, f"ECAN{i}_INT{self.letter}") or defaults[i]
            self._lines_irq[i] = line

    def reset(self) -> None:
        self._cancel_events()
        if self.chip is not None:
            self.can_bus.node_reset(self)
            self._loop_bus.node_reset(self)
        for i in (0, 1):
            if self._lines[i]:
                self._set_line(i, False)
        super().reset()
        self.errors.reset()
        self._cce = True
        self._pda = False
        self._sync_t = float("inf")
        self._tsc = (0, self.now_ns(), False, 0.0)

    def _cancel_events(self) -> None:
        if self.chip is not None:
            for ev in self._evs.values():
                self.chip.sched.cancel(ev)
        self._evs.clear()

    def _sched(self, key: str, t: float, cb) -> None:
        if self.chip is None:
            return
        self.chip.sched.cancel(self._evs.pop(key, None))
        self._evs[key] = self.chip.sched.at_ns(t, self._fire, key, cb)

    def _fire(self, key: str, cb) -> None:
        self._evs.pop(key, None)
        cb()

    def _unsched(self, key: str) -> None:
        ev = self._evs.pop(key, None)
        if ev is not None and self.chip is not None:
            self.chip.sched.cancel(ev)

    # ------------------------------------------------------------------ power-down read behaviour
    def read(self, offset: int, size: int) -> int:
        if self._pda:
            return PDA if offset == 0x18 else 0          # "CANES reads 0x08, all other registers 0x00"
        return C2000Block.read(self, offset, size)

    # ------------------------------------------------------------------ geometry / fields
    def _ecan_mode(self) -> bool:
        return bool(self.get("CANMC") & SCB)

    def _nmb(self) -> int:
        return 32 if self._ecan_mode() else 16

    def _mb(self, n: int, part: int) -> int:
        o = (0x100 + 8 * n + 2 * part) * 2
        return int.from_bytes(self.mem[o:o + 4], "little")

    def _set_mb(self, n: int, part: int, v: int) -> None:
        o = (0x100 + 8 * n + 2 * part) * 2
        self.mem[o:o + 4] = (v & M32).to_bytes(4, "little")

    def _wordreg(self, off: int) -> int:
        o = off * 2
        return int.from_bytes(self.mem[o:o + 4], "little")

    def _set_wordreg(self, off: int, v: int) -> None:
        o = off * 2
        self.mem[o:o + 4] = (v & M32).to_bytes(4, "little")

    def _bits(self, regname: str, set_bits: int = 0, clr_bits: int = 0) -> None:
        self.set(regname, (self.get(regname) | set_bits) & ~clr_bits)

    # ------------------------------------------------------------------ CAN node protocol (chipem.canbus)
    @property
    def can_name(self) -> str:
        return self.name

    def can_attach(self, bus: CanBus) -> None:
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
        return self._loop_bus if self.get("CANMC") & STM else self.can_bus

    def clock_hz(self) -> float:
        """eCAN module clock = SYSCLKOUT / 2 (§13.7.1.2)."""
        return float(self.chip.clock.hz) / 2.0 if self.chip is not None else 0.0

    def _btc_valid(self) -> bool:
        btc = self.get("CANBTC")
        return bool((btc >> 3) & 0xF) and bool(btc & 7)

    def can_bit_ns(self) -> float:
        btc = self.get("CANBTC")
        hz = self.clock_hz()
        if hz <= 0 or not self._btc_valid():
            return 0.0
        brp = ((btc >> 16) & 0xFF) + 1
        tq = (((btc >> 3) & 0xF) + 1) + ((btc & 7) + 1) + 1
        return 1e9 * brp * tq / hz

    def bitrate(self) -> float:
        b = self.can_bit_ns()
        return 1e9 / b if b > 0 else 0.0

    def _gate_on(self) -> bool:
        sc = self._sysctrl
        return sc is None or sc.clock_enabled(self.gate)

    def _on_bus(self, bus: CanBus) -> bool:
        if bus is not self._bus() or self._cce or self._pda or self.errors.busoff or not self._gate_on():
            return False
        return self.chip.now_ns() >= self._sync_t

    def can_tx_frame(self, bus: CanBus) -> CanFrame | None:
        if not self._on_bus(bus):
            return None
        n = self._best_tx()
        return None if n is None else self._frame_of(n)

    def can_tx_connected(self, bus: CanBus) -> bool:
        if bus is self._loop_bus:
            return True
        if not (self.get("CANTIOC") & FUNC and self.get("CANRIOC") & FUNC):
            return False
        return pin_reason(self.chip, self.tx_signal, self._pins) is None and \
            pin_reason(self.chip, self.rx_signal, self._pins) is None

    def can_rx_state(self, bus: CanBus) -> str | None:
        if not self._on_bus(bus):
            return None
        if bus is not self._loop_bus and (not self.get("CANRIOC") & FUNC
                                          or pin_reason(self.chip, self.rx_signal, self._pins) is not None):
            return None
        return "active"

    def can_error_passive(self) -> bool:
        return self.errors.state == "passive"

    def can_rx(self, bus: CanBus, frame: CanFrame, t_sof: float, t: float) -> None:
        if self._pda and self.get("CANMC") & WUBA and bus is self.can_bus:
            self._wake()                                  # the frame that woke us up is lost
            return
        if self.can_rx_state(bus) is None:
            return
        self._err_events(self.errors.rx_ok())
        self.rx_frames += 1
        self._receive(frame, t)

    def can_rx_error(self, bus: CanBus, kind: str, t: float) -> None:
        if self.can_rx_state(bus) is None or kind == "noack":
            return
        self._es_flag(FE)
        self._err_events(self.errors.rx_error())

    def can_tx_done(self, bus: CanBus, frame: CanFrame, ok: bool, err: str | None, t_sof: float, t: float) -> None:
        n = frame.tag
        bit = 1 << n if n is not None else 0
        if ok:
            self.tx_frames += 1
            self._err_events(self.errors.tx_ok())
            if bus is self._loop_bus:
                self._receive(frame, t, selftest=True)
            if bit and self.get("CANTRS") & bit:
                self._bits("CANTRS", clr_bits=bit)
                self._bits("CANTRR", clr_bits=bit)
                self._bits("CANTOS", clr_bits=bit)
                if not (self.get("CANMD") & bit):        # remote requests of receive mailboxes set no TA
                    self._bits("CANTA", set_bits=bit)
                self._stamp(n, t)
        else:
            self._es_flag(ACKE if err == "ack" else BE)
            self._err_events(self.errors.tx_error(err or "bit"))
            if bit and self.get("CANTRR") & bit:          # abort requested while on the bus: aborted now
                self._abort(n)
        self._update_irq()
        self._tx_changed()

    # ------------------------------------------------------------------ transmit side
    def _best_tx(self) -> int | None:
        trs = self.get("CANTRS") & self.get("CANME")
        if not trs:
            return None
        mc = self.get("CANMC")
        md = self.get("CANMD")
        ecan = bool(mc & SCB)
        best, bkey = None, None
        for n in range(self._nmb()):
            bit = 1 << n
            if not trs & bit:
                continue
            if mc & CDR and (mc & 0x1F) == n:
                continue                                  # CPU is changing its data
            if md & bit and not self._mb(n, 1) & 0x10:
                continue                                  # receive mailbox without RTR: TRS ignored
            key = (((self._mb(n, 1) >> 8) & 0x1F) if ecan else 0, n)
            if bkey is None or key > bkey:
                best, bkey = n, key
        return best

    def _frame_of(self, n: int) -> CanFrame:
        msgid, ctrl = self._mb(n, 0), self._mb(n, 1)
        ext = bool(msgid >> 31 & 1)
        ident = msgid & 0x1FFFFFFF if ext else (msgid >> 18) & 0x7FF
        mdl, mdh = self._mb(n, 2), self._mb(n, 3)
        order = "little" if self.get("CANMC") & DBO else "big"
        data = mdl.to_bytes(4, order) + mdh.to_bytes(4, order)
        return CanFrame(ident, data, ext=ext, rtr=bool(ctrl & 0x10), dlc=ctrl & 0xF, tag=n)

    def _tx_changed(self) -> None:
        if self.chip is None:
            return
        bus = self._bus()
        if self._on_bus(bus) and self._best_tx() is not None:
            bus.request(self)
        else:
            bus.withdraw(self)
        self._sched_timeout()

    def _abort(self, n: int) -> None:
        bit = 1 << n
        self._bits("CANTRS", clr_bits=bit)
        self._bits("CANTRR", clr_bits=bit)
        self._bits("CANTOS", clr_bits=bit)
        self._bits("CANAA", set_bits=bit)

    def _tx_hook(self, blk, reg, old, new):
        self.set(reg.name, new)
        self._update_irq()
        self._tx_changed()
        return None

    def _trs_write(self, blk, reg, old, new):
        self.set("CANTRS", old | new)                     # RS: writing 0 has no effect
        self._tx_changed()
        return old | new

    def _trr_write(self, blk, reg, old, new):
        trr = old | new
        self.set("CANTRR", trr)
        onair = self._bus().transmitting(self) if self.chip is not None else None
        busy = onair.tag if onair is not None else None
        trs = self.get("CANTRS")
        for n in range(32):
            bit = 1 << n
            if not trr & bit or n == busy:
                continue
            if trs & bit:
                self._abort(n)
            else:
                self._bits("CANTRR", clr_bits=bit)
        self._update_irq()
        self._tx_changed()
        return self.get("CANTRR")

    def _mb_writer(self, n: int, part: str):
        def hook(blk, reg, old, new):
            bit = 1 << n
            me, md = self.get("CANME") & bit, self.get("CANMD") & bit
            mc = self.get("CANMC")
            denied = False
            if part == "id":
                denied = bool(me)
            elif me and md:
                denied = True                             # enabled receive mailbox
            elif part == "data" and me and (self.get("CANTRS") | self.get("CANTRR")) & bit:
                denied = not (mc & CDR and (mc & 0x1F) == n)
            if denied:
                if not mc & CDR:
                    self._gif_set(WDIF)
                return old
            return None
        return hook

    # ------------------------------------------------------------------ receive side
    def _mask(self, n: int) -> int:
        if self._ecan_mode():
            return self.get(f"LAM{n}")
        if n >= 6:
            return self.get("CANGAM")
        return self.get("LAM3") if n >= 3 else self.get("LAM0")

    def _match(self, n: int, fr: CanFrame) -> bool:
        msgid = self._mb(n, 0)
        mask = self._mask(n) if msgid >> 30 & 1 else 0
        if not mask >> 31 & 1 and bool(msgid >> 31 & 1) != fr.ext:
            return False
        fid = fr.id if fr.ext else fr.id << 18
        cmp = (0x1FFFFFFF if fr.ext else 0x1FFC0000) & ~mask
        return not (msgid ^ fid) & cmp

    def _receive(self, fr: CanFrame, t: float, selftest: bool = False) -> None:
        me, md = self.get("CANME"), self.get("CANMD")
        if fr.rtr:
            for n in range(self._nmb() - 1, -1, -1):
                bit = 1 << n
                if not me & bit or not self._match(n, fr):
                    continue
                if not md & bit:
                    if not self._mb(n, 0) >> 29 & 1:
                        continue                          # transmit mailbox without AAM: not this one
                    self._bits("CANRFP", set_bits=bit)    # auto answer: TRS set, the data is sent back
                    self._bits("CANTRS", set_bits=bit)
                    self._tx_changed()
                    return
                self._store(n, fr, t, selftest)
                return
            self.rx_lost += 1
            return
        rmp, opc = self.get("CANRMP"), self.get("CANOPC")
        any_rx = False
        for n in range(self._nmb() - 1, -1, -1):
            bit = 1 << n
            if not me & bit or not md & bit:
                continue
            any_rx = True
            if not self._match(n, fr):
                continue
            if rmp & bit:
                if opc & bit:
                    continue                              # protected: try the next mailbox
                self._bits("CANRML", set_bits=bit)
            self._store(n, fr, t, selftest)
            return
        if selftest and not any_rx:
            self._store(0, fr, t, selftest)               # STM: no receive mailbox at all -> mailbox 0
            return
        self.rx_lost += 1

    def _store(self, n: int, fr: CanFrame, t: float, selftest: bool) -> None:
        bit = 1 << n
        if not selftest:
            msgid = self._mb(n, 0)
            self._set_mb(n, 0, (msgid & 0x60000000) | (int(fr.ext) << 31) | (fr.id if fr.ext else fr.id << 18))
        ctrl = self._mb(n, 1)
        self._set_mb(n, 1, (ctrl & 0x1F00) | (int(fr.rtr) << 4) | fr.dlc)
        data = fr.data + bytes(8 - len(fr.data))
        order = "little" if self.get("CANMC") & DBO else "big"
        self._set_mb(n, 2, int.from_bytes(data[:4], order))
        self._set_mb(n, 3, int.from_bytes(data[4:], order))
        self._bits("CANRMP", set_bits=bit)
        self._bits("CANTOS", clr_bits=bit)
        self._stamp(n, t)
        self._update_irq()
        self._sched_timeout()

    def _stamp(self, n: int, t: float) -> None:
        if not self._ecan_mode():
            return
        self.set(f"MOTS{n}", self._tsc_value(t))
        if n == 16 and self.get("CANMC") & MBCC:
            self._tsc_rebase(0)

    # ------------------------------------------------------------------ time stamp counter / time-outs
    def _tsc_value(self, t: float) -> int:
        v, t0, run, bit = self._tsc
        if not run or bit <= 0:
            return v
        return (v + int(max(0.0, t - t0) // bit)) & M32

    def _tsc_running(self) -> bool:
        return not self._cce and not self._pda

    def _tsc_rebase(self, value: int | None = None) -> None:
        now = self.now_ns()
        v = self._tsc_value(now) if value is None else value & M32
        self._tsc = (v, now, self._tsc_running(), self.can_bit_ns())
        self._sched_timeout()

    def _tsc_read(self, blk, reg):
        return self._tsc_value(self.now_ns())

    def _tsc_write(self, blk, reg, old, new):
        self._tsc_rebase(new)
        return None

    def _toc_write(self, blk, reg, old, new):
        self._bits("CANTOS", clr_bits=old & ~new)         # clearing TOC[n] clears TOS[n]
        self._update_irq(repulse=True)
        self._sched_timeout()
        return None

    def _moto_write(self, blk, reg, old, new):
        self._sched_timeout()
        return None

    def _sched_timeout(self) -> None:
        if self.chip is None:
            return
        toc = self.get("CANTOC") & ~self.get("CANTOS")
        v, t0, run, bit = self._tsc
        if not toc or not self._ecan_mode() or not run or bit <= 0:
            self._unsched("tmo")
            return
        md, trs = self.get("CANMD"), self.get("CANTRS")
        now = self.now_ns()
        tsc = self._tsc_value(now)
        best = None
        for n in range(32):
            bit_n = 1 << n
            if not toc & bit_n or (not md & bit_n and not trs & bit_n):
                continue
            d = (self.get(f"MOTO{n}") - tsc) & M32
            if d & 0x80000000:
                d = 0                                     # MOTO already passed
            if best is None or d < best:
                best = d
        if best is None:
            self._unsched("tmo")
            return
        t_due = t0 + (((tsc + best) - v) & M32) * bit if best else now
        self._sched("tmo", max(t_due, now), self._timeout_check)

    def _timeout_check(self) -> None:
        now = self.now_ns()
        tsc = self._tsc_value(now)
        toc, md, trs = self.get("CANTOC"), self.get("CANMD"), self.get("CANTRS")
        hit = 0
        for n in range(32):
            bit = 1 << n
            if toc & bit and (md & bit or trs & bit) and not ((self.get(f"MOTO{n}") - tsc) & M32) & 0x80000000 \
                    and tsc >= self.get(f"MOTO{n}"):
                hit |= bit
        if hit:
            self._bits("CANTOS", set_bits=hit)
            self._update_irq()
        self._sched_timeout()

    # ------------------------------------------------------------------ CANMC / CANBTC / modes
    def _init_only(self, blk, reg, old, new):
        return None if self._cce else old

    def _lam_scc_write(self, blk, reg, old, new):
        return None if (self._cce or self._ecan_mode()) else old

    def _btc_write(self, blk, reg, old, new):
        return None if self._cce else old                 # writable in initialization mode only

    def _mc_write(self, blk, reg, old, new):
        if self.enforce_eallow and not self.eallow() and (old ^ new) & MC_PROTECTED:
            if not self._warned_mc:
                self._warned_mc = True
                self.emit("warn", block=self.name, msg=f"{self.name}.CANMC: protected bits (15-9, 7-6) not written, "
                                                       "EALLOW is not set")
            new = (new & ~MC_PROTECTED) | (old & MC_PROTECTED)
        if new & SRES:
            self._soft_reset()
        new &= ~SRES
        if new & TCC:
            v = self._tsc_value(self.now_ns()) & 0x7FFFFFFF
            self._tsc_rebase(v)
            new &= ~TCC
        self.set("CANMC", new)
        changed = old ^ new
        if changed & CCR:
            if new & CCR:
                self._enter_init()
            else:
                self._leave_init()
        if changed & PDR:
            if new & PDR:
                self._pda = True
                self._sync_t = float("inf")
                self._tsc_rebase()
                if self.chip is not None:
                    self.can_bus.withdraw(self)
            else:
                self._wake(cpu=True)
        if changed & (STM | SCB | CDR | 0x1F | DBO):
            if changed & STM and self.chip is not None:
                (self.can_bus if new & STM else self._loop_bus).withdraw(self)
            self._tx_changed()
        self._update_irq()
        return new

    def _enter_init(self) -> None:
        self._unsched("cce")
        self._cce = True
        self._sync_t = float("inf")
        self._tsc_rebase()
        if self.chip is not None:
            self._bus().withdraw(self)

    def _leave_init(self) -> None:
        if self.chip is None:
            return
        bit = self.can_bit_ns()
        if bit <= 0:
            return                                        # invalid CANBTC: stays in initialization mode
        t = self.now_ns() + BUS_IDLE_BITS * bit
        if self.errors.busoff:
            t = max(t, self._boff_t + BUSOFF_RECOVERY_BITS * bit)
        self._sched("cce", t, self._init_done)

    def _init_done(self) -> None:
        if self.get("CANMC") & CCR:
            return
        self._cce = False
        self._err_events(self.errors.reset())             # "after leaving initialization mode the counters clear"
        self._sync_t = self.now_ns()
        self._tsc_rebase()
        self._tx_changed()

    def _wake(self, cpu: bool = False) -> None:
        if not self._pda and not self.get("CANMC") & PDR:
            return
        self._pda = False
        self.set("CANMC", self.get("CANMC") & ~PDR)
        if not cpu:
            self._gif_set(WUIF)
        bit = self.can_bit_ns()
        self._sync_t = self.now_ns() + BUS_IDLE_BITS * bit if bit > 0 else float("inf")
        self._tsc_rebase()
        if bit > 0:
            self._sched("sync", self._sync_t, self._tx_changed)

    def _soft_reset(self) -> None:
        for rn in ("CANME", "CANMD", "CANTRS", "CANTRR", "CANTA", "CANAA", "CANRMP", "CANRML", "CANRFP", "CANGAM",
                   "CANES", "CANGIF0", "CANGIF1", "CANMIL", "CANOPC", "CANTOC", "CANTOS"):
            self.set(rn, self.regs[rn].reset)
        if self.chip is not None:
            self._bus().node_reset(self)
        self._unsched("tmo")

    # ------------------------------------------------------------------ errors / status
    def _es_read(self, blk, reg):
        v = self.get("CANES") & ES_ERR
        if self._cce:
            v |= CCE
        if self._pda:
            v |= PDA
        if self._cce and not self.errors.busoff:
            v |= SA1                                      # never saw a recessive bit while off the bus
        st = self.errors.state
        if st == "busoff":
            v |= BO | EP | SA1
        elif st == "passive":
            v |= EP
        if self.errors.tec >= 96 or self.errors.rec >= 96:
            v |= EW
        if self.chip is not None and not self._cce:
            act = self._bus().activity(self, self.now_ns())
            v |= TM if act == "tx" else RM if act == "rx" else 0
        return v

    def _es_flag(self, bit: int) -> None:
        es = self.get("CANES")
        if not es & ES_ERR:                               # only the first error is stored
            self.set("CANES", es | bit)

    def _err_events(self, evs: set[str]) -> None:
        if not evs:
            return
        if "tx_warn" in evs or "rx_warn" in evs:
            self._gif_set(WLIF)
        if "passive" in evs:
            self._gif_set(EPIF)
        if "busoff" in evs:
            self._gif_set(BOIF)
            self._boff_t = self.now_ns()
            self.emit("info", block=self.name, msg=f"{self.name}: bus off (TEC > 255)")
            if self.chip is not None:
                self._bus().withdraw(self)
                bit = self.can_bit_ns()
                if self.get("CANMC") & ABO:
                    self._sched("boff", self._boff_t + BUSOFF_RECOVERY_BITS * bit, self._auto_bus_on)
                else:
                    self.set("CANMC", self.get("CANMC") | CCR)    # "CCR is also set upon a bus-off condition"
                    self._enter_init()
        self._update_irq()

    def _auto_bus_on(self) -> None:
        if self.errors.busoff:
            self._err_events(self.errors.recover())
            self.emit("info", block=self.name, msg=f"{self.name}: auto bus-on (ABO), error active again")
            self._tx_changed()

    # ------------------------------------------------------------------ interrupts
    def _gif_set(self, flag: int) -> None:
        line = 1 if self.get("CANGIM") & GIL else 0
        self._bits(f"CANGIF{line}", set_bits=flag)
        self._update_irq()

    def _derived(self, line: int) -> tuple[int, int]:
        """(derived GIF bits, MIV) of interrupt line `line`."""
        nmask = M32 if self._ecan_mode() else 0xFFFF
        mil = self.get("CANMIL")
        lvl = mil if line else ~mil & M32
        g = (self.get("CANTA") | self.get("CANRMP")) & self.get("CANMIM") & lvl & nmask
        v = GMIF if g else 0
        miv = g.bit_length() - 1 if g else 0
        if self.get("CANTOS") & lvl & nmask:
            v |= MTOF
        if (1 if self.get("CANGIM") & GIL else 0) == line:
            if self.get("CANAA"):
                v |= AAIF
            if self.get("CANRML"):
                v |= RMLIF
        return v, miv

    def _gif_reader(self, line: int):
        def hook(blk, reg):
            d, miv = self._derived(line)
            return (self.get(f"CANGIF{line}") & GIF_RC) | d | miv
        return hook

    def _line_level(self, line: int) -> bool:
        gim = self.get("CANGIM")
        if not gim & (I1EN if line else I0EN):
            return False
        d, _miv = self._derived(line)
        f = (self.get(f"CANGIF{line}") & GIF_RC) | d
        if f & GMIF:
            return True
        masks = ((MTOF, MTOM), (AAIF, AAIM), (RMLIF, RMLIM), (WLIF, WLIM), (EPIF, EPIM), (BOIF, BOIM),
                 (WUIF, WUIM), (WDIF, WDIM), (TCOF, TCOM))
        return any(f & fl and gim & m for fl, m in masks)

    def _set_line(self, line: int, level: bool) -> None:
        self._lines[line] = level
        irq = self._lines_irq[line]
        if irq is not None and self.chip is not None:
            self.irq(irq, level)

    def _update_irq(self, repulse: bool = False) -> None:
        if self.chip is None:
            return
        for line in (0, 1):
            lvl = self._line_level(line)
            if lvl != self._lines[line]:
                self._set_line(line, lvl)
            elif lvl and repulse:
                self._set_line(line, False)               # a flag was cleared, others still pending: new edge
                self._set_line(line, True)

    def _irq_hook(self, blk, reg, old, new):
        self.set(reg.name, new)
        self._update_irq()
        return None

    def _clear_hook(self, blk, reg, old, new):
        self.set(reg.name, new)
        self._update_irq(repulse=bool(old & ~new))
        if reg.name == "CANTOS":
            self._sched_timeout()
        return None

    def _rmp_write(self, blk, reg, old, new):
        cleared = old & ~new
        self.set("CANRMP", new)
        if cleared:
            self._bits("CANRML", clr_bits=cleared)        # clearing RMP[n] clears RML[n] (and RMLIF)
        self._update_irq(repulse=bool(cleared))
        self._sched_timeout()
        return None

    # ------------------------------------------------------------------ reporting
    def can_stats(self) -> dict:
        st = "power-down" if self._pda else "init" if self._cce else self.errors.state
        return {"bitrate": self.bitrate(), "tx": self.tx_frames, "rx": self.rx_frames, "lost": self.rx_lost,
                "tec": self.errors.tec & 0xFF, "rec": self.errors.rec, "state": st,
                "bus": self.can_bus.name if self.can_bus is not self._own_bus else None}
