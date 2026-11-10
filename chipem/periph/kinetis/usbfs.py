"""Kinetis K22 USB full-speed OTG controller (USB0, K22P100M120SF5V2RM ch. 43 "USBFSOTG") in device mode, on a
chipem.usbbus cable (`kinetis.usbfs`, 0x40072000).

Register file (8-bit registers, SVD table USB0; reset values from the SVD):
  0x000 PERID 0x04 / 0x004 IDCOMP 0xFB / 0x008 REV 0x33 / 0x00C ADDINFO 0x01 (read-only IDs), 0x010 OTGISTAT (w1c),
  0x014 OTGICR, 0x018 OTGSTAT, 0x01C OTGCTL, 0x080 ISTAT (w1c), 0x084 INTEN, 0x088 ERRSTAT (w1c), 0x08C ERREN,
  0x090 STAT (read-only window on the 4-entry token status FIFO), 0x094 CTL, 0x098 ADDR, 0x09C/0x0B0/0x0B4
  BDTPAGE1/2/3, 0x0A0/0x0A4 FRMNUML/H, 0x0A8 TOKEN, 0x0AC SOFTHLD, 0x0C0 + 4n ENDPT0..15, 0x100 USBCTRL (0xC0),
  0x104 OBSERVE (0x50, live), 0x108 CONTROL, 0x10C USBTRC0, 0x114 USBFRMADJUST.

Behaviour (device mode, as the NXP KHCI device driver uses it):
* Buffer Descriptor Table (43.3): base = BDTPAGE3:BDTPAGE2:BDTPAGE1[7:1] << 9 (512-byte aligned); BD address =
  base | ENDP << 5 | TX << 4 | ODD << 3.  BD word 0: OWN (7), DATA0/1 (6), KEEP / TOK_PID[3] (5), NINC / TOK_PID[2]
  (4), DTS / TOK_PID[1] (3), BDT_STALL / TOK_PID[0] (2), BC (25:16); word 1 = buffer address.  The SIE reads the BD
  when a token for an enabled endpoint direction arrives: OWN = 0 -> NAK; BDT_STALL -> STALL (the BD is not
  consumed and ENDPTn.EPSTALL is set); else the data moves with chip.bus (NINC: no address increment) and, unless
  KEEP = 1, the BD is written back (BC = bytes received / sent, TOK_PID = 0x1 OUT / 0x9 IN / 0xD SETUP, OWN = 0),
  the endpoint direction's ODD ping-pong bit flips, STAT = ENDP << 4 | TX << 3 | ODD << 2 is queued and ISTAT.TOKDNE
  is set.  IN data is read from memory at the IN token and committed (write-back, ODD flip, TOKDNE) when the host
  acknowledges it (usb_in_done).  The PID sent is DATA0/1 from the BD.
* DTS = 1 on a receive BD: a DATA0/1 PID different from the BD's DATA0/1 bit is acknowledged and ignored (BD left
  owned, no TOKDNE): a retransmission after a lost ACK.  SETUP is always accepted regardless of DATA0/1 (it is
  always DATA0 and re-synchronises the toggle, USB 2.0 8.5.3).  More data than BC: clipped to BC, ACKed,
  ERRSTAT.DMAERR, BC written back = BC (Table 43-6).  A BD buffer outside mapped memory: ERRSTAT.DMAERR, NAK, no
  write-back (memory-latency case of Table 43-6).
* STAT FIFO (43.4.13): STAT is valid while TOKDNE = 1; up to 3 more completed tokens wait in the holding FIFO (4 in
  all); clearing TOKDNE (w1c) loads the next one and sets TOKDNE again at once, or clears STAT.  While the FIFO is
  full the SIE NAKs tokens that would need a BD.
* SETUP: needs ENDPTn with EPRXEN = EPTXEN = 1 and EPCTLDIS = 0; on completion CTL.TXSUSPENDTOKENBUSY is set and the
  SIE NAKs every token (SETUP included) until firmware clears it (43.4.14).
* ENDPTn: EPRXEN (OUT/SETUP), EPTXEN (IN), EPCTLDIS (Table 43-93): a token for a disabled endpoint direction is not
  answered.  EPSTALL: STALL for any token on the endpoint (SETUP included, as the RM text says "any access") and
  ISTAT.STALL; firmware clears EPSTALL (the NXP driver does it in its STALL interrupt).  EPHSHK = 0 (isochronous)
  is not modelled: one `warn`, tokens answered as with EPHSHK = 1.
* Address: tokens are answered for ADDR[6:0] only.  A USB reset sets ISTAT.USBRST (2.5 us into SE0, once per reset)
  and clears ADDR (43.4.15).  The ODD ping-pong bits are reset only by CTL.ODDRST (held while ODDRST = 1) and by
  setting CTL.USBENSOFEN (43.4.14), not by a bus reset (the NXP driver sets ODDRST in its reset handler).
* Bus states: 3 ms idle -> ISTAT.SLEEP; resume K -> ISTAT.RESUME; SOF -> ISTAT.SOFTOK and FRMNUML/H.  CTL.RESUME set
  while suspended = remote wakeup signalling (bus.remote_wakeup()).  USBTRC0.USBRESMEN: resume K while suspended sets
  USB_RESUME_INT (the asynchronous wakeup interrupt, part of the USB0 line) and SYNC_DET; clearing USBRESMEN clears
  both.  OTGISTAT.ONEMSEC: 1 ms timer while the module is enabled.  CTL.JSTATE / SE0 are live line states.
* Connection: the D+ pull-up (usb_pullup "fs") is on when HOSTMODEEN = 0 and either OTGCTL.OTGEN = 1 and
  OTGCTL.DPHIGH = 1, or OTGEN = 0, CTL.USBENSOFEN = 1 and CONTROL.DPPULLUPNONOTG = 1 (43.2.2, 43.4.8, 43.4.26); the
  bus adds VBUS (USB0 has no VBUS sense pin: VBUS feeds VREGIN).  OBSERVE.DPPU mirrors it, DPPD/DMPD = USBCTRL.PDE
  (or OTGCTL.DPLOW/DMLOW with OTGEN).  USBCTRL.SUSP = 1 (its reset value) puts the transceiver in suspend: the
  pull-up stays (as USB suspend requires) but the SIE neither receives nor sends packets (one `warn` when a token
  is lost that way).  SIM_SOPT1.USBREGEN = 0 when the pull-up turns on: one `warn` (VOUT33 must then come from
  outside; assumed present).
* Clocks: SIM_SCGC4[USBOTG] gate (a register access with the gate off logs one `warn`; bus traffic is ignored).
  The 48 MHz USB clock: SIM_SOPT2.USBSRC = 1 -> MCGPLLCLK (PLLFLLSEL = 1; PLL running: C6.PLLS or C5.PLLCLKEN0) or
  MCGFLLCLK x (USBFRAC + 1) / (USBDIV + 1) (SIM_CLKDIV2); USBSRC = 0 -> USB_CLKIN (param `usb_clkin_hz`, default 0 =
  no clock).  Not within 48 MHz +-0.25 %: the SIE does not answer the bus (the host times out: "device not
  recognised") and one `warn` explains why.
* USBTRC0.USBRESET: module hard reset (all registers to reset values), reads 0.  CTL.HOSTMODEEN / ADDR.LSEN / TOKEN
  writes (host mode) log one `warn`: host mode is not modelled (registers are storage; HOSTMODEEN disconnects).
* IRQ "USB0" (by name): (ISTAT & INTEN) | (OTGISTAT & OTGICR) | USBTRC0.USB_RESUME_INT; ISTAT.ERROR is set when
  an ERRSTAT bit enabled in ERREN is set.
Not modelled: host mode, OTG comparators (OTGSTAT/OTGISTAT session and ID bits are storage: the K22 has no USB0
VBUS/ID pins), isochronous endpoints, low speed, CRC/bit-stuff/PID errors (the cable is error-free), SOFTHLD /
USBFRMADJUST (host only).
"""
from __future__ import annotations

from collections import deque
from typing import Any, Callable

from ...bus import BusError
from ..registry import register
from ._util import GatedBlock, build_regs, find_model, fmt_hz, irq_number

# ISTAT / INTEN
USBRST, ERROR, SOFTOK, TOKDNE, SLEEP, RESUME, ATTACH, STALL = (1 << b for b in range(8))
# ERRSTAT
PIDERR, CRC5EOF, CRC16, DFN8, BTOERR, DMAERR, BTSERR = 1, 2, 4, 8, 16, 32, 128
# CTL
USBENSOFEN, ODDRST, CTL_RESUME, HOSTMODEEN, CTL_RESET, TXSUSPENDTOKENBUSY, SE0, JSTATE = (1 << b for b in range(8))
# ENDPTn
EPHSHK, EPSTALL, EPTXEN, EPRXEN, EPCTLDIS = 1, 2, 4, 8, 16
# USBCTRL
PDE, SUSP = 0x40, 0x80
# OTGCTL
OTGEN, DMLOW, DPLOW, DPHIGH = 0x04, 0x10, 0x20, 0x80
# OTGISTAT
ONEMSEC = 0x40
# CONTROL
DPPULLUPNONOTG = 0x10
# USBTRC0
USB_RESUME_INT, SYNC_DET, USBRESMEN, USBRESET = 0x01, 0x02, 0x20, 0x80
# BD
BD_OWN, BD_DATA1, BD_KEEP, BD_NINC, BD_DTS, BD_STALL = 0x80, 0x40, 0x20, 0x10, 0x08, 0x04

PID_OUT, PID_IN, PID_SETUP = 0x1, 0x9, 0xD
USB_TOL = 0.0025
STAT_DEPTH = 4                 # STAT + 3 holding entries (43.4.13: "a four byte FIFO")

# SIM bits
SOPT1_USBREGEN = 1 << 31
SOPT2_USBSRC = 1 << 18
SOPT2_PLLFLLSEL = 1 << 16


@register("kinetis.usbfs")
class UsbFs(GatedBlock):
    """USBFS OTG controller in device mode (see the module doc)."""

    gate = "USBOTG"

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        irqw = {"on_write": self._irq_write}
        conn = {"on_write": self._conn_write}
        ro = {"ro_mask": 0xFF}
        ov: dict[str, dict] = {
            "PERID": ro, "IDCOMP": ro, "REV": ro, "ADDINFO": ro,
            "OTGISTAT": {"w1c_mask": 0xED, "ro_mask": 0x12, "on_read": self._otgistat_read,
                         "on_write": self._otgistat_write},
            "OTGICR": {"ro_mask": 0x12, "on_write": self._otgicr_write},
            "OTGSTAT": {"ro_mask": 0xFF},
            "OTGCTL": {"ro_mask": 0x4B, **conn},
            "ISTAT": {"w1c_mask": 0xFF, "on_write": self._istat_write},
            "INTEN": irqw,
            "ERRSTAT": {"w1c_mask": 0xBF, "ro_mask": 0x40, **irqw},
            "ERREN": {"ro_mask": 0x40, **irqw},
            "STAT": {"ro_mask": 0xFF},
            "CTL": {"ro_mask": JSTATE | SE0, "on_read": self._ctl_read, "on_write": self._ctl_write},
            "ADDR": {"on_write": self._addr_write},
            "BDTPAGE1": {"ro_mask": 0x01},
            "FRMNUML": {"ro_mask": 0xFF}, "FRMNUMH": {"ro_mask": 0xFF},
            "TOKEN": {"on_write": self._token_write},
            "USBCTRL": {"ro_mask": 0x3F, **conn},
            "OBSERVE": {"ro_mask": 0xFF, "on_read": self._observe_read},
            "CONTROL": {"ro_mask": 0xEF, **conn},
            "USBTRC0": {"ro_mask": 0x5F, "on_write": self._usbtrc0_write},
        }
        for n in range(16):
            ov[f"ENDPT{n}"] = {"ro_mask": 0x20 if n == 0 else 0xE0, "on_write": self._endpt_write}
        r = build_regs((name, "USB0"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.usb_name = name
        self.bus = None                        # chipem.usbbus.UsbBus
        self.vbus_listeners: list[Callable[[bool], None]] = []
        self._vbus = False
        self._odd = [0] * 32                   # ping-pong bit per (endpoint, TX)
        self._stat_q: deque[int] = deque()      # STAT holding FIFO (behind the STAT register)
        self._in_pending: dict[int, tuple[int, int, int, int]] = {}
        self._bus_suspended = False
        self._line = False
        self._irqn: int | None = None
        self._mcg: Any = None
        self._warned: set[str] = set()
        self._ms_t0 = 0.0
        self._ms_seen = 0
        self._ms_ev = None
        self._sof_ev = None
        self._pull = False
        self.stats = {"setup": 0, "out": 0, "in": 0, "nak": 0, "stall": 0, "sof": 0, "resets": 0}

    # ------------------------------------------------------------------ attach / reset
    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irqn = irq_number(chip, "USB0", self.name)
        self._mcg = find_model(chip, "kinetis.mcg", "MCG")

    def _cancel_events(self) -> None:
        sched = getattr(self.chip, "sched", None)
        for nm in ("_ms_ev", "_sof_ev"):
            ev = getattr(self, nm)
            if ev is not None and sched is not None:
                sched.cancel(ev)
            setattr(self, nm, None)

    def reset(self) -> None:
        self._cancel_events()
        super().reset()
        self._odd = [0] * 32
        self._stat_q.clear()
        self._in_pending.clear()
        self._ms_seen = 0
        self._pull = False
        if self.chip is not None:
            self._update_irq()
        if self.bus is not None:
            self.bus.device_changed()

    # ------------------------------------------------------------------ helpers
    def _r(self, off: int) -> int:
        return self.mem[off]

    def _w(self, off: int, v: int) -> None:
        self.mem[off] = v & 0xFF

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=msg)

    def _update_irq(self) -> None:
        if self.chip is None or self._irqn is None:
            return
        m = self.mem
        line = bool(m[0x80] & m[0x84]) or bool(m[0x10] & m[0x14]) or bool(m[0x10C] & USB_RESUME_INT)
        if line != self._line:
            self._line = line
            self.irq(self._irqn, line)

    def _irq_write(self, blk, reg, old, new):
        self._update_irq()
        return None

    def _istat(self, bits: int) -> None:
        self.mem[0x80] |= bits
        self._update_irq()

    def _err(self, bits: int) -> None:
        self.mem[0x88] |= bits
        if bits & self.mem[0x8C]:
            self.mem[0x80] |= ERROR
        self._update_irq()

    # ------------------------------------------------------------------ register hooks
    def _istat_write(self, blk, reg, old, new):
        if old & TOKDNE and not new & TOKDNE:
            if self._stat_q:
                self._w(0x90, self._stat_q.popleft())
                new |= TOKDNE                                # the next status is there at once
            else:
                self._w(0x90, 0)
            self._w(0x80, new)
        self._update_irq()
        return new

    def _otgistat_read(self, blk, reg) -> int:
        self._onemsec()
        return self.mem[0x10]

    def _otgistat_write(self, blk, reg, old, new):
        if old & ONEMSEC and not new & ONEMSEC:
            self._ms_seen = self._ms_count()
        self._update_irq()
        return None

    def _otgicr_write(self, blk, reg, old, new):
        self._arm_ms()
        self._update_irq()
        return None

    def _ms_count(self) -> int:
        if self.chip is None or not self.mem[0x94] & USBENSOFEN:
            return self._ms_seen
        return int((self.chip.now_ns() - self._ms_t0) // 1e6)

    def _onemsec(self) -> None:
        if self._ms_count() > self._ms_seen and not self.mem[0x10] & ONEMSEC:
            self.mem[0x10] |= ONEMSEC
            self._update_irq()

    def _arm_ms(self) -> None:
        """The 1 ms timer only needs scheduler events while its interrupt is enabled (else it is updated on read)."""
        if self.chip is None:
            return
        if self._ms_ev is not None:
            self.chip.sched.cancel(self._ms_ev)
            self._ms_ev = None
        if self.mem[0x14] & ONEMSEC and self.mem[0x94] & USBENSOFEN:
            k = self._ms_count() + 1
            self._ms_ev = self.chip.sched.at_ns(self._ms_t0 + k * 1e6, self._ms_tick)

    def _ms_tick(self) -> None:
        self._ms_ev = None
        self._onemsec()
        self._arm_ms()

    def _ctl_read(self, blk, reg) -> int:
        v = self.mem[0x94] & ~(JSTATE | SE0)
        b = self.bus
        if b is None or b.state == "detached" or b.state == "reset":
            v |= SE0
        else:
            v |= JSTATE                                      # full speed idle = J (D+ high)
        return v

    def _ctl_write(self, blk, reg, old, new):
        if new & USBENSOFEN and not old & USBENSOFEN:
            self._odd = [0] * 32                             # 43.4.14: setting USBENSOFEN resets the ODD bits
            if self.chip is not None:
                self._ms_t0 = self.chip.now_ns()
            self._ms_seen = 0
            self._clock_ok(report=True)
        if new & ODDRST:
            self._odd = [0] * 32
        if new & HOSTMODEEN:
            self._warn_once("host", f"{self.name}: CTL.HOSTMODEEN set: USB host mode is not modelled (the "
                            f"controller leaves the bus; host registers are storage)")
        if new & CTL_RESUME and not old & CTL_RESUME and self._bus_suspended and self.bus is not None \
                and not new & HOSTMODEEN:
            self.bus.remote_wakeup()
        self._arm_ms()
        self._connection()
        self._update_irq()
        return None

    def _addr_write(self, blk, reg, old, new):
        if new & 0x80:
            self._warn_once("lsen", f"{self.name}: ADDR.LSEN set: low-speed tokens are a host-mode feature "
                            f"(not modelled); ignored")
        return None

    def _token_write(self, blk, reg, old, new):
        self._warn_once("token", f"{self.name}: TOKEN written: host-mode token execution is not modelled")
        return None

    def _endpt_write(self, blk, reg, old, new):
        if new & (EPRXEN | EPTXEN) and not new & EPHSHK:
            self._warn_once(f"iso{reg.name}", f"{self.name}: {reg.name}.EPHSHK = 0 (isochronous endpoint): not "
                            f"modelled, the endpoint keeps handshaking")
        return None

    def _conn_write(self, blk, reg, old, new):
        self._connection()
        return None

    def _usbtrc0_write(self, blk, reg, old, new):
        if new & USBRESET:
            self.reset()                                     # hard reset of the USB module; USBRESET reads 0
            return self.mem[0x10C]
        v = new & ~USBRESET
        if not v & USBRESMEN:
            v &= ~(USB_RESUME_INT | SYNC_DET)
        self._w(0x10C, v)
        self._update_irq()
        return v

    def _observe_read(self, blk, reg) -> int:
        otg = self.mem[0x1C]
        v = 0x80 if self._pullup_enabled() else 0
        if otg & OTGEN:
            v |= (0x40 if otg & DPLOW else 0) | (0x10 if otg & DMLOW else 0)
        elif self.mem[0x100] & PDE or self.mem[0x94] & HOSTMODEEN:
            v |= 0x50
        return v

    # ------------------------------------------------------------------ connection / clocks
    def _pullup_enabled(self) -> bool:
        m = self.mem
        if m[0x94] & HOSTMODEEN:
            return False
        otg = m[0x1C]
        if otg & OTGEN:
            return bool(otg & DPHIGH)
        return bool(m[0x94] & USBENSOFEN and m[0x108] & DPPULLUPNONOTG)

    def _connection(self) -> None:
        on = self._pullup_enabled()
        if on and not self._pull and self.chip is not None:
            sim = self._sim
            if sim is not None and not sim.get("SOPT1") & SOPT1_USBREGEN:
                self._warn_once("vreg", f"{self.name}: D+ pull-up on with the USB voltage regulator disabled "
                                f"(SIM_SOPT1.USBREGEN = 0): the transceiver's 3.3 V (VOUT33) must be supplied "
                                f"externally; assumed present")
        self._pull = on
        if self.bus is not None:
            self.bus.device_changed()

    def usb_clock_hz(self) -> float:
        """The USB module's 48 MHz clock as selected by SIM_SOPT2.USBSRC / PLLFLLSEL and SIM_CLKDIV2."""
        sim = self._sim
        if sim is None:
            return float(self.params.get("clk48_hz", 48e6))
        sopt2 = sim.get("SOPT2")
        if not sopt2 & SOPT2_USBSRC:
            return float(self.params.get("usb_clkin_hz", 0.0))
        mcg = self._mcg
        if mcg is None:
            return float(self.params.get("clk48_hz", 48e6))
        if sopt2 & SOPT2_PLLFLLSEL:
            running = (mcg.get("C6") & 0x40 or mcg.get("C5") & 0x40) and mcg._osc_ok()
            src = mcg.pll_hz() if running else 0.0
        else:
            src = 0.0 if mcg.mode in ("BLPI", "BLPE") else mcg.fll_hz()
        div2 = sim.get("CLKDIV2")
        return src * ((div2 & 1) + 1) / (((div2 >> 1) & 7) + 1)

    def _clock_desc(self) -> str:
        sim = self._sim
        if sim is None:
            return "clk48_hz"
        sopt2 = sim.get("SOPT2")
        if not sopt2 & SOPT2_USBSRC:
            return "USB_CLKIN (SIM_SOPT2.USBSRC = 0)"
        src = "MCGPLLCLK" if sopt2 & SOPT2_PLLFLLSEL else "MCGFLLCLK"
        div2 = sim.get("CLKDIV2")
        return f"{src} x {(div2 & 1) + 1}/{((div2 >> 1) & 7) + 1} (SIM_CLKDIV2 USBFRAC/USBDIV)"

    def _clock_ok(self, report: bool = False) -> bool:
        if self.chip is None:
            return False
        f = self.usb_clock_hz()
        if abs(f - 48e6) > USB_TOL * 48e6:
            what = "stopped" if f <= 0 else fmt_hz(f)
            self._warn_once(f"clk{round(f)}", f"{self.name}: the USB 48 MHz clock from {self._clock_desc()} is "
                            f"{what}: it must be 48 MHz +-0.25 %; the controller does not answer the host")
            return False
        return True

    def _gate_on(self) -> bool:
        loc = self._gate_loc
        return loc is None or bool(self._sim.mem[loc[0]] & loc[1])

    def _sie_runs(self) -> bool:
        """The SIE takes part in bus events (reset / suspend / SOF detection)."""
        m = self.mem
        return (self.chip is not None and bool(m[0x94] & USBENSOFEN) and not m[0x94] & HOSTMODEEN
                and self._gate_on() and self._clock_ok())

    def _ready(self, x) -> bool:
        """Answer a token? (module enabled and clocked, transceiver awake, our address)."""
        if self.chip is None:
            return False
        m = self.mem
        if not m[0x94] & USBENSOFEN or m[0x94] & HOSTMODEEN:
            return False
        if not self._gate_on():
            self._warn_once("gate", f"{self.name}: bus traffic while the USB clock gate SIM_SCGC4[USBOTG] is off: "
                            f"ignored")
            return False
        if not self._clock_ok():
            return False
        if m[0x100] & SUSP:
            self._warn_once("susp", f"{self.name}: token received while the transceiver is suspended "
                            f"(USBCTRL.SUSP = 1, its reset value): not answered")
            return False
        return x.addr == (m[0x98] & 0x7F)

    # ------------------------------------------------------------------ BDT
    def bdt_base(self) -> int:
        m = self.mem
        return (m[0xB4] << 24) | (m[0xB0] << 16) | ((m[0x9C] & 0xFE) << 8)

    def _bd(self, ep: int, tx: int, odd: int) -> int:
        return self.bdt_base() | (ep << 5) | (tx << 4) | (odd << 3)

    def _push_stat(self, ep: int, tx: int, odd: int) -> None:
        st = (ep << 4) | (tx << 3) | (odd << 2)
        if not self.mem[0x80] & TOKDNE and not self._stat_q:
            self._w(0x90, st)
            self._istat(TOKDNE)
        else:
            self._stat_q.append(st)

    def _stat_full(self) -> bool:
        return bool(self.mem[0x80] & TOKDNE) and len(self._stat_q) >= STAT_DEPTH - 1

    def _flip(self, ep: int, tx: int) -> None:
        if not self.mem[0x94] & ODDRST:
            self._odd[ep * 2 + tx] ^= 1

    def _writeback(self, bd: int, ctrl: int, n: int, pid: int) -> None:
        if ctrl & BD_KEEP:
            return
        v = (ctrl & BD_DATA1) | ((n & 0x3FF) << 16) | ((pid & 0xF) << 2)
        if ctrl & BD_NINC:
            v |= BD_NINC                                     # 43.3.4: NINC = 1 leaves bit 4 unchanged
        self.chip.bus.write(bd, 4, v)

    def _endpoint(self, ep: int) -> int:
        return self.mem[0xC0 + 4 * ep]

    def _stall(self) -> str:
        self.stats["stall"] += 1
        self._istat(STALL)
        return "STALL"

    def _nak(self) -> str:
        self.stats["nak"] += 1
        return "NAK"

    def _receive(self, x, pid: int) -> str | None:
        """SETUP / OUT + DATAx into the endpoint's current RX BD."""
        ep = x.ep & 0xF
        e = self._endpoint(ep)
        if pid == PID_SETUP:
            if not (e & EPRXEN and e & EPTXEN) or e & EPCTLDIS:
                return None
        elif not e & EPRXEN:
            return None
        if e & EPSTALL:
            return self._stall()
        if self.mem[0x94] & TXSUSPENDTOKENBUSY or self._stat_full():
            return self._nak()
        odd = self._odd[ep * 2]
        bd = self._bd(ep, 0, odd)
        bus = self.chip.bus
        try:
            ctrl = bus.read(bd, 4)
            if not ctrl & BD_OWN:
                return self._nak()
            if ctrl & BD_STALL:
                self._w(0xC0 + 4 * ep, e | EPSTALL)
                return self._stall()
            got = 1 if x.pid == "DATA1" else 0
            if pid == PID_OUT and ctrl & BD_DTS and got != (1 if ctrl & BD_DATA1 else 0):
                return "ACK"                                 # toggle mismatch: duplicate, ACK and ignore
            bc = (ctrl >> 16) & 0x3FF
            data = bytes(x.data)
            if len(data) > bc:
                data = data[:bc]
                self._err(DMAERR)
            addr = bus.read(bd + 4, 4)
            if data:
                if ctrl & BD_NINC:
                    for b in data:
                        bus.write(addr, 1, b)
                else:
                    bus.write_bytes(addr, data)
            self._writeback(bd, ctrl, len(data), pid)
        except BusError as exc:
            self._err(DMAERR)
            self._warn_once("dma", f"{self.name}: BD / buffer access failed ({exc}): DMAERR, token NAKed")
            return self._nak()
        if not ctrl & BD_KEEP:
            self._flip(ep, 0)
            self._push_stat(ep, 0, odd)
        if pid == PID_SETUP:
            self.stats["setup"] += 1
            self._w(0x94, self.mem[0x94] | TXSUSPENDTOKENBUSY)
            self._in_pending.pop(ep, None)
        else:
            self.stats["out"] += 1
        self._update_irq()
        return "ACK"

    # ------------------------------------------------------------------ usbbus device protocol
    def usb_attach(self, bus) -> None:
        self.bus = bus

    def usb_detach(self) -> None:
        self.bus = None
        self._vbus = False
        for cb in list(self.vbus_listeners):
            cb(False)

    def usb_pullup(self, bus) -> str | None:
        return "fs" if self._pullup_enabled() else None

    def usb_vbus(self, bus, on: bool, t: float) -> None:
        self._vbus = bool(on)
        if not on:
            self._bus_suspended = False
        for cb in list(self.vbus_listeners):
            cb(self._vbus)

    def usb_reset(self, bus, active: bool, t: float) -> None:
        if not active:
            return
        self._bus_suspended = False
        if not self._sie_runs():
            return
        self.stats["resets"] += 1
        self._w(0x98, 0)                                     # 43.4.15: a decoded USB reset clears ADDR
        self._in_pending.clear()
        self._istat(USBRST)

    def usb_suspend(self, bus, t: float) -> None:
        self._bus_suspended = True
        if self._sie_runs():
            self._istat(SLEEP)

    def usb_resume(self, bus, t: float) -> None:
        if not self._bus_suspended:
            return
        self._bus_suspended = False
        m = self.mem
        sync = self._sie_runs()
        if m[0x10C] & USBRESMEN:
            m[0x10C] |= USB_RESUME_INT | (SYNC_DET if sync else 0)
        if sync:
            m[0x80] |= RESUME
        self._update_irq()

    def usb_sof(self, bus, frame: int, t: float) -> None:
        if not self._sie_runs():
            return
        self.stats["sof"] += 1
        self._w(0xA0, frame & 0xFF)
        self._w(0xA4, (frame >> 8) & 7)
        self._istat(SOFTOK)
        pb = getattr(self.chip, "pins", None)
        if pb is not None:
            pins = pb.signal_pins("USB_SOF_OUT")
            if pins and any(pb.signal_matches(p.owner, "USB_SOF_OUT") for p in pins):
                pb.drive_signal("USB_SOF_OUT", "H")
                if self._sof_ev is not None:
                    self.chip.sched.cancel(self._sof_ev)
                self._sof_ev = self.chip.sched.in_ns(1e3, self._sof_low)

    def _sof_low(self) -> None:
        self._sof_ev = None
        self.chip.pins.drive_signal("USB_SOF_OUT", "L")

    def usb_setup(self, bus, x) -> str | None:
        if not self._ready(x):
            return None
        return self._receive(x, PID_SETUP)

    def usb_out(self, bus, x) -> str | None:
        if not self._ready(x):
            return None
        return self._receive(x, PID_OUT)

    def usb_in(self, bus, x):
        if not self._ready(x):
            return None
        ep = x.ep & 0xF
        e = self._endpoint(ep)
        if not e & EPTXEN:
            return None
        if e & EPSTALL:
            return self._stall()
        if self.mem[0x94] & TXSUSPENDTOKENBUSY or self._stat_full():
            return self._nak()
        odd = self._odd[ep * 2 + 1]
        bd = self._bd(ep, 1, odd)
        bus = self.chip.bus
        try:
            ctrl = bus.read(bd, 4)
            if not ctrl & BD_OWN:
                return self._nak()
            if ctrl & BD_STALL:
                self._w(0xC0 + 4 * ep, e | EPSTALL)
                return self._stall()
            bc = (ctrl >> 16) & 0x3FF
            addr = bus.read(bd + 4, 4)
            if ctrl & BD_NINC:
                data = bytes(bus.read(addr, 1) for _ in range(bc))
            else:
                data = bus.read_bytes(addr, bc) if bc else b""
        except BusError as exc:
            self._err(DMAERR)
            self._warn_once("dma", f"{self.name}: BD / buffer access failed ({exc}): DMAERR, token NAKed")
            return self._nak()
        self._in_pending[ep] = (bd, odd, ctrl, bc)
        return ("DATA1" if ctrl & BD_DATA1 else "DATA0", data)

    def usb_in_done(self, bus, x, acked: bool, t: float) -> None:
        ep = x.ep & 0xF
        p = self._in_pending.pop(ep, None)
        if p is None or not acked or self.chip is None:
            return
        bd, odd, ctrl, bc = p
        self.stats["in"] += 1
        try:
            self._writeback(bd, ctrl, bc, PID_IN)
        except BusError:
            self._err(DMAERR)
            return
        if not ctrl & BD_KEEP:
            self._flip(ep, 1)
            self._push_stat(ep, 1, odd)
        self._update_irq()

    # ------------------------------------------------------------------ status
    def usb_stats(self) -> dict:
        return dict(self.stats, address=self.mem[0x98] & 0x7F, suspended=self._bus_suspended,
                    connected=bool(self.bus is not None and self.bus.state != "detached"))
