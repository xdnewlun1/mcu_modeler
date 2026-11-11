"""STM32F103 USB full-speed device controller (`stm32f1.usb`, 0x40005C00) and its packet memory (`stm32f1.pma`,
0x40006000), on a chipem.usbbus cable.

Verified against RM0008 Rev 21 ch. 23 "Universal serial bus full-speed device interface (USB)" (23.3-23.5).

Packet memory (23.3.1, 23.5): 512 bytes = 256 16-bit words, shared with bxCAN on the performance line; the USB sees
local addresses 0..0x1FF, the CPU (APB1) each word in the low half of a 32-bit slot: CPU address = 0x40006000 +
2 x local address.  The upper half of each slot reads 0 and ignores writes; a byte access reaches the low (offset 0) or
high (offset 1) byte of the word.  Not cleared by a reset (SRAM; zero at power-on here).

Registers (23.5; 16 significant bits at 32-bit stride, bits 31:16 read 0):
  EPnR (n = 0..7, 23.5.2): CTR_RX (15, rc_w0: "only 0 can be written"), DTOG_RX (14, t), STAT_RX (13:12, t), SETUP (11,
      r), EP_TYPE (10:9), EP_KIND (8), CTR_TX (7, rc_w0), DTOG_TX (6, t), STAT_TX (5:4, t), EA (3:0); t = toggle: writing
      1 flips, 0 keeps.  A byte write leaves the other byte's bits at their "invariant" values (CTR = 1, t = 0).
  CNTR (reset 0x0003 FRES | PDWN; bits 7:5 reserved), ISTR (14:8 rc_w0; CTR (15), DIR (4), EP_ID (3:0) read-only,
  computed), FNR (read-only FN, LSOF, LCK, RXDM, RXDP), DADDR (EF, ADD; 15:8 reserved), BTABLE (15:3; 2:0 forced 0).
  Buffer descriptor table at BTABLE, 8 bytes per EPnR: ADDRn_TX, COUNTn_TX, ADDRn_RX, COUNTn_RX (BL_SIZE (15): 0 =
  2-byte blocks, size = 2 x NUM_BLOCK; 1 = 32-byte blocks, size = 32 x (NUM_BLOCK + 1); COUNT (9:0) written by the USB).

Verified behaviour (RM0008 section):
* Tokens are handled only with DADDR.EF = 1 and a matching ADD and EA (23.5.1 DADDR), and not while ISTR.RESET is set
  ("Reception and transmission are disabled until the RESET bit is cleared", 23.5.1 ISTR).
* SETUP (23.4.2 "Control transfers", 23.5.2): control endpoints only; accepted (ACK) whatever STAT_RX is unless DISABLED;
  discarded without a handshake while CTR_RX is still set; data to the RX buffer, COUNT, CTR_RX = SETUP = 1, DTOG_TX = 1,
  DTOG_RX cleared and toggled by the accepted DATA0 (= 1), STAT_RX = STAT_TX = NAK.
* OUT (23.4.2): STAT_RX DISABLED -> no answer, STALL -> STALL, NAK -> NAK; VALID: a DATAx PID different from DTOG_RX is
  ACKed and ignored (no CTR); a packet longer than the RX buffer is copied only up to the end of the buffer and answered
  STALL without CTR_RX (buffer overrun); a control endpoint with STATUS_OUT (EP_KIND) STALLs an OUT with data
  (23.5.2 EP_KIND).  Else COUNT, CTR_RX = 1, DTOG_RX toggles, STAT_RX = NAK; SETUP is cleared unless CTR_RX was still 1
  ("kept frozen while CTR_RX is at 1").
* IN (23.4.2): STAT_TX DISABLED -> no answer, STALL, NAK; VALID: COUNTn_TX bytes from ADDRn_TX with DATA<DTOG_TX>; on
  the host's ACK: DTOG_TX toggles, STAT_TX = NAK, CTR_TX = 1.
* Double-buffered bulk (23.4.3 Tables 169/170): EP_TYPE = bulk + EP_KIND (DBL_BUF); the USB uses buffer DTOG (DTOG_RX
  for OUT, DTOG_TX for IN; buffer 0 = the ADDRn_TX/COUNTn_TX words, buffer 1 = ADDRn_RX/COUNTn_RX), SW_BUF is the other
  DTOG bit; DTOG == SW_BUF answers NAK.  The first transaction after DBL_BUF is set ends like a plain one (STAT -> NAK);
  after it STAT stays VALID and only the DTOG bit toggles.
* Endpoint reset (23.5.2): a USB reset or FRES clears every EPnR except CTR_RX/CTR_TX, and DADDR; CNTR, BTABLE and the
  PMA are kept (23.5.1 RESET).  FRES = 1 sets ISTR.RESET and holds the SIE in reset until cleared.
* Frames (23.5.1 SOF/ESOF, FNR): SOF sets ISTR.SOF, FNR.FN, LCK after two consecutive SOFs, LSOF = 0.  A frame timer
  expects a SOF 1 ms after the last one (from the end of a USB reset): each missed SOF sets ESOF and counts FNR.LSOF
  (saturating at 3), the third consecutive one sets SUSP (23.4.5: no traffic for 3 ms) unless FSUSP = 1 (the suspend
  check is off in suspend mode); ESOF keeps coming every 1 ms (it times the RESUME pulse, 23.4.5).  LCK is lost on suspend
  and reset.
* Wakeup (23.5.1 WKUP, 23.4.5): resume or reset signalling while in suspend mode (CNTR.FSUSP = 1) sets ISTR.WKUP, clears
  LP_MODE and pulses USB_WAKEUP (EXTI line 18).  CNTR.RESUME 0 -> 1 while the bus is suspended = remote wakeup
  (bus.remote_wakeup()); a RESUME pulse shorter than 1 ms or longer than 15 ms, or RESUME with FSUSP = 0, logs one
  `warn` (23.5.1 RESUME, 23.4.5 note).  FNR.RXDP/RXDM (Table 172): J (10) idle, SE0 in reset, K while resuming.
* ISTR (23.5.1): EP_ID is the endpoint register with a pending CTR, isochronous and double-buffered bulk ones first, then
  the others, lowest register first; DIR = 1 when CTR_RX (or both) is set.
* Interrupts (23.3.1 "Interrupt Mapper"): USB_LP_CAN1_RX0 (20) = (CTRM and any CTR) or (ISTR[14:8] & CNTR[14:8]);
  USB_HP_CAN1_TX (19) = CTRM and a CTR on an isochronous or double-buffered bulk endpoint; both shared with bxCAN
  (`shared_irq` "USB" share).  USBWakeUp (42) through EXTI line 18.
* PDWN (23.5.1): the transceiver is off; the device needs the RCC USB clock, PDWN = 0 and FRES = 0 to answer.  APB1
  below 8 MHz while the USB is in use logs one `warn` (23.4.2 note).

Connection (the F103 has no internal D+ pull-up: boards fit 1.5 k to 3.3 V, fixed or switched): attached when the
transceiver is on (RCC_APB1ENR.USBEN and CNTR.PDWN = 0), CNTR.FRES = 0, USBCLK is 48 MHz +-0.25 % and the pull-up is
present: param `pullup` = "always" (default, a fixed resistor as on the "blue pill"), "none", or a pin name ("PA8":
high connects; "!PA8": low connects).  USBCLK not 48 MHz: one `warn`, no connection.  `transceiver_on()` gives
PA11/PA12 to USBDM/USBDP (stm32f1.gpio).  RCC_APB1RSTR.USBRST resets the block (registers; the PMA keeps its content).

Approximate / not modelled:
* Isochronous endpoints (23.4.4): the usbbus has no isochronous transfers; tokens are answered as for bulk (one `warn`).
* ERR and PMAOVR never occur (error-free cable; PMA arbitration and the APB1 bandwidth are not modelled; 23.5.1).
* The SETUP-with-STAT_RX = NAK case: 23.4.2 says the SETUP is accepted, the EP_TYPE description of 23.5.2 says a control
  endpoint "defined as NAK" does not answer; the model accepts it (USB 2.0 8.5.3).
* PMA accesses are not checked against the USB/CAN clock; using USB and bxCAN together only warns (the shared SRAM is not
  corrupted).  The transceiver start-up time tSTARTUP after PDWN = 0 (23.4.2) is not enforced.
"""
from __future__ import annotations

from typing import Any

from ..base import MMIOBlock
from ..registry import register
from ._util import GatedBlock, apb_hz, build_regs, find_model, fmt_hz, gpio_ports, irq_number, rcc, shared_irq
from . import _util

# EPnR
CTR_RX, DTOG_RX, STAT_RX, SETUP, EP_TYPE, EP_KIND = 0x8000, 0x4000, 0x3000, 0x0800, 0x0600, 0x0100
CTR_TX, DTOG_TX, STAT_TX, EA = 0x0080, 0x0040, 0x0030, 0x000F
EP_RW = EP_TYPE | EP_KIND | EA
EP_T = DTOG_RX | STAT_RX | DTOG_TX | STAT_TX
EP_RC = CTR_RX | CTR_TX
DISABLED, STALL, NAK, VALID = 0, 1, 2, 3
BULK, CONTROL, ISO, INTERRUPT = 0, 1, 2, 3
# CNTR
FRES, PDWN, LP_MODE, FSUSP, RESUME = 1, 2, 4, 8, 16
ESOFM, SOFM, RESETM, SUSPM, WKUPM, ERRM, PMAOVRM, CTRM = (1 << b for b in range(8, 16))
# ISTR flags (same positions as the masks)
I_ESOF, I_SOF, I_RESET, I_SUSP, I_WKUP, I_ERR, I_PMAOVR, I_CTR = ESOFM, SOFM, RESETM, SUSPM, WKUPM, ERRM, PMAOVRM, CTRM
ISTR_FLAGS = 0x7F00
# FNR
LCK, RXDM, RXDP = 1 << 13, 1 << 14, 1 << 15

OFF_CNTR, OFF_ISTR, OFF_FNR, OFF_DADDR, OFF_BTABLE = 0x40, 0x44, 0x48, 0x4C, 0x50
USB_TOL = 0.0025
PMA_BYTES = 512
NEP = 8
FRAME_NS = 1_000_000.0                   # a SOF is expected every 1 ms (23.5.1 ESOF)
ESOF_SLACK_NS = 2_000.0                  # tolerance before a missing SOF counts as lost
APB1_MIN_HZ = 8e6                        # 23.4.2 note: APB1 must run above 8 MHz


@register("stm32f1.pma")
class PacketMemory(MMIOBlock):
    """512-byte USB/CAN packet memory: 256 16-bit words, each in the low half of a 32-bit CPU slot."""

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        super().__init__(name, base, size, (), ram_like=False, log=log, params=params)
        self.data = bytearray(PMA_BYTES)

    def reset(self) -> None:
        self._seen.clear()                       # SRAM: content survives resets

    @staticmethod
    def _local(offset: int) -> int | None:
        """PMA byte address of CPU byte offset `offset`, or None for the dead upper half of a slot."""
        if offset & 2:
            return None
        return ((offset >> 2) << 1 | (offset & 1)) & (PMA_BYTES - 1)

    def read(self, offset: int, size: int) -> int:
        d = self.data
        v = 0
        for i in range(size):
            a = self._local(offset + i)
            if a is not None:
                v |= d[a] << (8 * i)
        if self.log_all:
            self.emit("mmio", block=self.name, addr=self.base + offset, reg=None, size=size, access="r", value=v)
        return v

    def write(self, offset: int, size: int, value: int) -> None:
        d = self.data
        for i in range(size):
            a = self._local(offset + i)
            if a is not None:
                d[a] = (value >> (8 * i)) & 0xFF
        if self.log_all:
            self.emit("mmio", block=self.name, addr=self.base + offset, reg=None, size=size, access="w", value=value)

    def peek_bytes(self, offset: int, n: int) -> tuple[bytes, list[bool]]:
        return bytes(self.read(offset + i, 1) for i in range(n)), [True] * n

    # USB-side access (local addresses)
    def r16(self, a: int) -> int:
        a &= PMA_BYTES - 2
        return self.data[a] | self.data[a + 1] << 8

    def w16(self, a: int, v: int) -> None:
        a &= PMA_BYTES - 2
        self.data[a] = v & 0xFF
        self.data[a + 1] = (v >> 8) & 0xFF

    def read_bytes(self, a: int, n: int) -> bytes:
        return bytes(self.data[(a + i) & (PMA_BYTES - 1)] for i in range(n))

    def write_bytes(self, a: int, data: bytes) -> None:
        for i, b in enumerate(data):
            self.data[(a + i) & (PMA_BYTES - 1)] = b


@register("stm32f1.usb")
class UsbFsDevice(GatedBlock):
    """F1 USB full-speed device controller (see the module doc)."""

    gate = "USB"

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        params.setdefault("reserved_zero", True)        # reserved offsets read 0, writes ignored
        hi = 0xFFFF0000
        ov: dict[str, dict] = {f"EP{n}R": {"ro_mask": hi} for n in range(NEP)}
        ov.update({
            "CNTR": {"ro_mask": hi | 0xE0, "on_write": self._cntr_write},
            "ISTR": {"ro_mask": hi | 0x80FF, "on_read": self._istr_read, "on_write": self._istr_write},
            "FNR": {"ro_mask": 0xFFFFFFFF, "on_read": self._fnr_read},
            "DADDR": {"ro_mask": hi | 0xFF00},
            "BTABLE": {"ro_mask": hi | 0x7},
        })
        r = build_regs((name, "USB"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.usb_name = name
        self.bus = None
        self._pma: PacketMemory | None = None
        self._irq_lp: int | None = None
        self._irq_hp: int | None = None
        self._lines = (False, False)
        self._warned: set[str] = set()
        self._bus_suspended = False
        self._sofs = 0
        self._in_pending: dict[int, tuple[int, int]] = {}      # n -> (buffer, dbl)
        self._dbl_live: set[int] = set()     # double-buffered bulk EPnR whose special flow control is active (23.4.3)
        self._esof_ev = None                 # frame timer: ESOF when an expected SOF does not come (23.5.1)
        self._lost = 0                       # consecutive lost SOFs (FNR.LSOF, saturates at 3)
        self._resume_at: float | None = None
        self._xcvr = False
        self._active = False
        self._pull_pin: str | None = None
        self._pull_low = False
        p = str(params.get("pullup", "always"))
        if p not in ("always", "none"):
            self._pull_low = p.startswith("!")
            self._pull_pin = p.lstrip("!")
        self.stats = {"setup": 0, "out": 0, "in": 0, "nak": 0, "stall": 0, "sof": 0, "resets": 0}

    pure_reads = frozenset({"ISTR", "FNR"})

    # ------------------------------------------------------------------ attach / reset
    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irq_lp = irq_number(chip, "USB_LP_CAN1_RX0", "USB_LP_CAN_RX0", "USB_LP")
        self._irq_hp = irq_number(chip, "USB_HP_CAN1_TX", "USB_HP_CAN_TX", "USB_HP")
        r = rcc(chip)
        if r is not None:
            if hasattr(r, "listeners"):
                r.listeners.append(self._connection)
            if hasattr(r, "gate_listeners"):
                r.gate_listeners.append(self._gate_changed)
        if self._pull_pin is not None:
            pb = getattr(chip, "pins", None)
            try:
                pin = pb.get(self._pull_pin) if pb is not None else None
            except Exception:  # noqa: BLE001 - unknown pin name
                pin = None
            if pin is None:
                self._warn_once("pullpin", f"{self.name}: pullup pin {self._pull_pin!r} not found on this chip: "
                                f"no D+ pull-up (the device never connects)")
            else:
                self._pull_pin = pin.id
                pb.input_listeners.setdefault(pin.id, []).append(lambda _p: self._connection())

    def reset(self) -> None:
        self._stop_frame_timer()
        super().reset()
        self._in_pending.clear()
        self._dbl_live.clear()
        self._bus_suspended = False
        self._sofs = 0
        self._lost = 0
        self._resume_at = None
        if self.chip is not None:
            self._update_irq()
            self._connection()

    # ------------------------------------------------------------------ helpers
    def _g(self, off: int) -> int:
        m = self.mem
        return m[off] | m[off + 1] << 8

    def _s(self, off: int, v: int) -> None:
        self.mem[off] = v & 0xFF
        self.mem[off + 1] = (v >> 8) & 0xFF

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.emit("warn", block=self.name, msg=msg)

    @property
    def pma(self) -> PacketMemory | None:
        p = self._pma
        if p is None and self.chip is not None:
            p = find_model(self.chip, "stm32f1.pma", "USB_CAN_SRAM")
            self._pma = p
        return p

    # ------------------------------------------------------------------ state
    def _clocked(self) -> bool:
        loc = self._gate_loc
        return loc is None or bool(self._rcc.mem[loc[0]] & loc[1])

    def active(self) -> bool:
        """USB in use: clock enabled and not powered down (bxCAN checks this: they share the packet SRAM)."""
        return self.chip is not None and self._clocked() and not self._g(OFF_CNTR) & PDWN

    def transceiver_on(self) -> bool:
        """The analog transceiver drives PA11/PA12 (USBDM/USBDP)."""
        return self.active()

    def usb_clock_hz(self) -> float:
        r = self._rcc
        if r is None or not hasattr(r, "usb_hz"):
            return float(self.params.get("clk48_hz", 48e6))
        return float(r.usb_hz())

    def _clock_ok(self, report: bool = False) -> bool:
        f = self.usb_clock_hz()
        if abs(f - 48e6) > USB_TOL * 48e6:
            if report:
                what = "stopped (PLL off)" if f <= 0 else fmt_hz(f)
                self._warn_once(f"clk{round(f)}", f"{self.name}: USBCLK is {what}: it must be 48 MHz +-0.25 % "
                                f"(PLL 72 MHz with RCC_CFGR.USBPRE = 0, or PLL 48 MHz with USBPRE = 1); the device "
                                f"does not connect")
            return False
        return True

    def _pullup_present(self) -> bool:
        p = self.params.get("pullup", "always")
        if p == "always":
            return True
        if p == "none" or self._pull_pin is None or self.chip is None:
            return False
        try:
            lv = self.chip.pins.get(self._pull_pin).level()
        except Exception:  # noqa: BLE001
            return False
        return lv == ("L" if self._pull_low else "H")

    def _sie_runs(self) -> bool:
        return self.active() and not self._g(OFF_CNTR) & FRES and self._clock_ok()

    def _connected(self) -> bool:
        if not self.active() or self._g(OFF_CNTR) & FRES or not self._pullup_present():
            return False
        return self._clock_ok(report=True)

    def _gate_changed(self, enr: str, old: int, new: int) -> None:
        if enr in ("APB1ENR",):
            self._connection()

    def _connection(self) -> None:
        if self.chip is None:
            return
        x = self.transceiver_on()
        if x != self._xcvr:
            self._xcvr = x
            for p in gpio_ports(self.chip):
                p.resync_all()
        a = self.active()
        if a and not self._active:
            self._check_can()
            if apb_hz(self.chip, 1) < APB1_MIN_HZ * 0.9999:
                self._warn_once("apb1", f"{self.name}: PCLK1 {fmt_hz(apb_hz(self.chip, 1))} is below 8 MHz: RM0008 "
                                f"23.4.2 requires APB1 > 8 MHz for the packet memory (data over/underrun)")
        if not a:
            self._stop_frame_timer()
        self._active = a
        if self.bus is not None:
            self.bus.device_changed()

    def _check_can(self) -> None:
        r = self._rcc
        if r is None or not hasattr(r, "clock_enabled") or not r.clock_enabled("CAN"):
            return
        can = (getattr(self.chip, "peripherals", None) or {}).get("CAN")
        act = getattr(can, "active", None)
        if act is not None and act():
            self._warn_once("can", f"{self.name}: USB and bxCAN share the 512-byte SRAM; using both is not supported "
                            f"by the silicon (the packet memory content is undefined)")

    # ------------------------------------------------------------------ IRQ
    def _ctr_endpoint(self) -> int | None:
        """EP_ID priority of 23.5.1 ISTR: isochronous and double-buffered bulk EPnR first, then the others; the lowest
        register number within a set."""
        m = self.mem
        other = None
        for n in range(NEP):
            v = m[4 * n] | m[4 * n + 1] << 8
            if v & EP_RC:
                t = (v >> 9) & 3
                if t == ISO or (t == BULK and v & EP_KIND):
                    return n
                if other is None:
                    other = n
        return other

    def _ctr_state(self) -> tuple[bool, bool]:
        anyc = hp = False
        m = self.mem
        for n in range(NEP):
            o = 4 * n
            v = m[o] | m[o + 1] << 8
            if v & EP_RC:
                anyc = True
                t = (v >> 9) & 3
                if t == ISO or (t == BULK and v & EP_KIND):
                    hp = True
                    break
        return anyc, hp

    def _update_irq(self) -> None:
        if self.chip is None:
            return
        cntr = self._g(OFF_CNTR)
        lp = bool(self._g(OFF_ISTR) & cntr & ISTR_FLAGS)
        hp = False
        if cntr & CTRM:
            anyc, hp = self._ctr_state()
            lp = lp or anyc
        if (lp, hp) != self._lines:
            if lp != self._lines[0]:
                shared_irq(self.chip, self._irq_lp, "USB", lp)
            if hp != self._lines[1]:
                shared_irq(self.chip, self._irq_hp, "USB", hp)
            self._lines = (lp, hp)

    def _flag(self, bits: int) -> None:
        self._s(OFF_ISTR, self._g(OFF_ISTR) | bits)
        self._update_irq()

    # ------------------------------------------------------------------ register access
    def write(self, offset: int, size: int, value: int) -> None:
        if self.chip is not None and self._gate_loc is not None:
            self._gate_ok()                                  # one `warn` if the USB clock is off
        if offset < 4 * NEP:
            self._ep_write(offset, size, value)
            return
        super().write(offset, size, value)

    def _ep_write(self, offset: int, size: int, value: int) -> None:
        sub = offset & 3
        if sub >= 2:
            return                                           # reserved upper half
        o = offset & ~3
        old = self._g(o)
        if size >= 2:
            lane, val = 0xFFFF, value & 0xFFFF
        else:
            sh = 8 * sub
            lane, val = 0xFF << sh, (value & 0xFF) << sh
        neutral = (old & EP_RW) | EP_RC                      # keep everything for bytes not written
        v = (neutral & ~lane) | (val & lane)
        new = (old & SETUP) | (v & EP_RW) | (old & v & EP_RC) | ((old ^ v) & EP_T)
        self._s(o, new)
        if (old ^ new) & (EP_TYPE | EP_KIND):
            self._dbl_live.discard(o >> 2)               # DBL_BUF (re)set: the next transaction is a plain one
        if self.log_all:
            self.emit("mmio", block=self.name, addr=self.base + offset, reg=f"EP{o >> 2}R", size=size, access="w",
                      value=value)
        if (new >> 9) & 3 == ISO and (new & (STAT_RX | STAT_TX)):
            self._warn_once(f"iso{o >> 2}", f"{self.name}: EP{o >> 2}R is isochronous: the usbbus has no "
                            f"isochronous transfers (answered as bulk)")
        if (old ^ new) & EP_RC:
            self._update_irq()

    def _cntr_write(self, blk, reg, old, new):
        if new & FRES and not old & FRES:
            self._usb_reset_state()
            self._flag(I_RESET)
        if new & RESUME and not old & RESUME:
            if not new & FSUSP:
                self._warn_once("resume_fsusp", f"{self.name}: CNTR.RESUME set while FSUSP = 0; RM0008 23.4.5: use "
                                "RESUME only after the peripheral was put in suspend mode (FSUSP = 1)")
            self._resume_at = self.chip.now_ns() if self.chip is not None else None
            if self._bus_suspended and self.bus is not None:
                self.bus.remote_wakeup()
        if old & RESUME and not new & RESUME and self._resume_at is not None and self.chip is not None:
            d = self.chip.now_ns() - self._resume_at
            self._resume_at = None
            if not 1e6 <= d <= 15e6:
                self._warn_once("resume_len", f"{self.name}: CNTR.RESUME held {d / 1e6:.3g} ms; RM0008 23.5.1 "
                                "requires 1 to 15 ms")
        if (old ^ new) & (FRES | PDWN):
            self._connection()
        self._update_irq()
        return None

    def _istr_read(self, blk, reg) -> int:
        v = self._g(OFF_ISTR) & ISTR_FLAGS
        n = self._ctr_endpoint()
        if n is not None:
            e = self._g(4 * n)
            v |= I_CTR | n                                   # EP_ID: the endpoint register number
            if e & CTR_RX:
                v |= 0x10                                    # DIR = 1: CTR_RX (or both) pending
        return v

    def _istr_write(self, blk, reg, old, new):
        v = old & new & ISTR_FLAGS                           # rc_w0
        self._s(OFF_ISTR, v)
        self._update_irq()
        return v

    def _fnr_read(self, blk, reg) -> int:
        v = self._g(OFF_FNR) & 0x3FFF
        b = self.bus
        st = b.state if b is not None else "detached"
        if st == "detached":
            pass
        elif st == "reset":
            pass                                             # SE0
        elif st == "resume":
            v |= RXDM                                        # K
        else:
            v |= RXDP                                        # J (idle, full speed)
        return v

    def _usb_reset_state(self) -> None:
        """USB reset / FRES (23.5.2 EPnR): DADDR and every EPnR cleared except CTR_RX / CTR_TX ("kept unchanged to
        avoid missing a correct packet notification immediately followed by an USB reset"), FNR lock lost."""
        for n in range(NEP):
            self._s(4 * n, self._g(4 * n) & EP_RC)
        self._s(OFF_DADDR, 0)
        self._s(OFF_FNR, self._g(OFF_FNR) & 0x7FF)
        self._sofs = 0
        self._lost = 0
        self._in_pending.clear()
        self._dbl_live.clear()

    # ------------------------------------------------------------------ frame timer (ESOF / SUSP, 23.5.1)
    def _stop_frame_timer(self) -> None:
        if self._esof_ev is not None and self.chip is not None:
            self.chip.sched.cancel(self._esof_ev)
        self._esof_ev = None

    def _arm_frame_timer(self, t_ns: float) -> None:
        self._stop_frame_timer()
        if self.chip is not None and self._sie_runs():
            self._esof_ev = self.chip.sched.at_ns(t_ns + FRAME_NS + ESOF_SLACK_NS, self._esof)

    def _esof(self) -> None:
        """An expected SOF did not come: ESOF, FNR.LSOF + 1; the third consecutive one sets SUSP unless the suspend
        check is off (FSUSP = 1).  The timer keeps running every 1 ms (ESOF times the RESUME pulse, 23.4.5)."""
        self._esof_ev = None
        if not self._sie_runs():
            return
        self._lost = min(self._lost + 1, 3)
        self._s(OFF_FNR, (self._g(OFF_FNR) & ~(3 << 11)) | (self._lost << 11))
        bits = I_ESOF
        if self._lost == 3 and not self._g(OFF_CNTR) & FSUSP and not self._g(OFF_ISTR) & I_SUSP and \
                not getattr(self, "_susp_reported", False):
            bits |= I_SUSP
            self._susp_reported = True
            self._sofs = 0
            self._s(OFF_FNR, self._g(OFF_FNR) & ~LCK)
        self._flag(bits)
        self._esof_ev = self.chip.sched.at_ns(self.chip.now_ns() + FRAME_NS, self._esof)

    # ------------------------------------------------------------------ BTABLE / PMA
    def _bd(self, n: int) -> int:
        return (self._g(OFF_BTABLE) & 0xFFF8) + 8 * n

    @staticmethod
    def _rx_size(count: int) -> int:
        nb = (count >> 10) & 0x1F
        return 32 * (nb + 1) if count & 0x8000 else 2 * nb

    def _buf(self, n: int, which: int) -> tuple[int, int]:
        """(address, count-word address) of buffer 0 (TX pair) or 1 (RX pair) of endpoint register n."""
        bd = self._bd(n)
        pma = self.pma
        if which == 0:
            return pma.r16(bd) & 0xFFFE, bd + 2
        return pma.r16(bd + 4) & 0xFFFE, bd + 6

    # ------------------------------------------------------------------ usbbus device protocol
    def usb_attach(self, bus) -> None:
        self.bus = bus

    def usb_detach(self) -> None:
        self.bus = None

    def usb_pullup(self, bus) -> str | None:
        return "fs" if self._connected() else None

    def usb_vbus(self, bus, on: bool, t: float) -> None:
        if not on:
            self._bus_suspended = False                      # no VBUS sense on the F103: nothing else

    def _wake(self) -> None:
        """23.5.1 WKUP: activity "during suspend mode" (CNTR.FSUSP = 1) clears LP_MODE and pulses USB_WAKEUP."""
        if not self._g(OFF_CNTR) & FSUSP:
            return
        self._s(OFF_CNTR, self._g(OFF_CNTR) & ~LP_MODE)
        self._flag(I_WKUP)
        _util.exti_line(self.chip, 18, True)
        _util.exti_line(self.chip, 18, False)

    def usb_reset(self, bus, active: bool, t: float) -> None:
        if not active:                       # end of the reset: the suspend check starts (23.5.1 SUSP)
            if self._sie_runs():
                self._arm_frame_timer(t)
            return
        was = self._bus_suspended
        self._bus_suspended = False
        if not self._sie_runs():
            return
        if was:
            self._wake()
        self.stats["resets"] += 1
        self._usb_reset_state()
        self._susp_reported = False
        self._flag(I_RESET)
        self._stop_frame_timer()

    def usb_suspend(self, bus, t: float) -> None:
        self._bus_suspended = True
        if self._sie_runs() and self._esof_ev is None and not getattr(self, "_susp_reported", False):
            # no frame timer running (no SOF / reset seen): report the three missed SOFs at once
            self._sofs = 0
            self._lost = 3
            self._susp_reported = True
            self._s(OFF_FNR, (self._g(OFF_FNR) & 0x7FF) | (3 << 11))
            self._flag(I_SUSP | I_ESOF)

    def usb_resume(self, bus, t: float) -> None:
        if not self._bus_suspended:
            return
        self._bus_suspended = False
        self._susp_reported = False
        if self._sie_runs():
            self._wake()

    def usb_sof(self, bus, frame: int, t: float) -> None:
        if not self._sie_runs():
            return
        self.stats["sof"] += 1
        self._sofs += 1
        self._lost = 0
        self._susp_reported = False
        self._s(OFF_FNR, (frame & 0x7FF) | (LCK if self._sofs >= 2 else 0))     # LSOF cleared
        self._flag(I_SOF)
        self._arm_frame_timer(t)

    def _match(self, x) -> int | None:
        if self.chip is None or getattr(self, "lp_frozen", False) or not self._sie_runs():
            return None                                      # also: USBEN off, powered down, FRES, no 48 MHz
        da = self._g(OFF_DADDR)
        if not da & 0x80 or x.addr != da & 0x7F:
            return None
        if self._g(OFF_ISTR) & I_RESET:
            return None                                      # 23.5.1 RESET: no transfers until RESET is cleared
        if self.pma is None:
            self._warn_once("pma", f"{self.name}: no stm32f1.pma block: the packet memory is missing")
            return None
        m = self.mem
        for n in range(NEP):
            if m[4 * n] & EA == x.ep:
                return n
        return None

    def _nak(self) -> str:
        self.stats["nak"] += 1
        return "NAK"

    def _stall(self) -> str:
        self.stats["stall"] += 1
        return "STALL"

    def usb_setup(self, bus, x) -> str | None:
        n = self._match(x)
        if n is None:
            return None
        e = self._g(4 * n)
        if (e >> 9) & 3 != CONTROL or (e & STAT_RX) == 0:
            return None
        if e & CTR_RX:
            return None          # 23.4.2: CTR_RX still pending -> the SETUP is discarded without a handshake
        addr, cnt = self._buf(n, 1)
        pma = self.pma
        size = self._rx_size(pma.r16(cnt))
        data = bytes(x.data)
        if len(data) > size:
            pma.write_bytes(addr, data[:size])               # copied up to the last allocated location
            return self._stall()
        pma.write_bytes(addr, data)
        pma.w16(cnt, (pma.r16(cnt) & 0xFC00) | len(data))
        self._s(4 * n, (e & (EP_RW | CTR_TX)) | CTR_RX | SETUP | DTOG_RX | DTOG_TX | (NAK << 12) | (NAK << 4))
        self._in_pending.pop(n, None)
        self.stats["setup"] += 1
        self._update_irq()
        return "ACK"

    def usb_out(self, bus, x) -> str | None:
        n = self._match(x)
        if n is None:
            return None
        o = 4 * n
        e = self._g(o)
        st = (e >> 12) & 3
        if st == DISABLED:
            return None
        if st == STALL:
            return self._stall()
        if st == NAK:
            return self._nak()
        typ = (e >> 9) & 3
        data = bytes(x.data)
        got = 1 if x.pid == "DATA1" else 0
        dbl = typ == BULK and bool(e & EP_KIND)
        if typ == CONTROL and e & EP_KIND and data:
            return self._stall()                             # STATUS_OUT: only a zero-length OUT is accepted
        tog = 1 if e & DTOG_RX else 0
        live = dbl and n in self._dbl_live
        if live and tog == (1 if e & DTOG_TX else 0):
            return self._nak()                               # both buffers held by the application
        if typ != ISO and got != tog:
            return "ACK"                                     # retransmission after a lost ACK: ignored
        addr, cnt = self._buf(n, tog if dbl else 1)
        pma = self.pma
        size = self._rx_size(pma.r16(cnt))
        if len(data) > size:                                 # 23.4.2: buffer overrun -> STALL, no CTR, data
            pma.write_bytes(addr, data[:size])               # copied only up to the last allocated location
            self._warn_once(f"ovr{n}", f"{self.name}: EP{n}R OUT packet of {len(data)} bytes, RX buffer holds "
                            f"{size}: buffer overrun, STALL")
            return self._stall()
        pma.write_bytes(addr, data)
        pma.w16(cnt, (pma.r16(cnt) & 0xFC00) | len(data))
        new = (e & ~SETUP) | CTR_RX if not e & CTR_RX else e   # SETUP is frozen while CTR_RX = 1 (23.5.2)
        new ^= DTOG_RX
        if not live and typ != ISO:
            new = (new & ~STAT_RX) | (NAK << 12)
        if dbl:
            self._dbl_live.add(n)            # 23.4.3: the first transaction after DBL_BUF ends like a plain one
        self._s(o, new)
        self.stats["out"] += 1
        self._update_irq()
        return "ACK"

    def usb_in(self, bus, x):
        n = self._match(x)
        if n is None:
            return None
        e = self._g(4 * n)
        st = (e >> 4) & 3
        if st == DISABLED:
            return None
        if st == STALL:
            return self._stall()
        if st == NAK:
            return self._nak()
        typ = (e >> 9) & 3
        dbl = typ == BULK and bool(e & EP_KIND)
        live = dbl and n in self._dbl_live
        tog = 1 if e & DTOG_TX else 0
        if live and tog == (1 if e & DTOG_RX else 0):
            return self._nak()                               # no buffer filled by the application
        addr, cnt = self._buf(n, tog if dbl else 0)
        pma = self.pma
        count = pma.r16(cnt) & 0x3FF
        data = pma.read_bytes(addr, count)
        self._in_pending[n] = (tog, int(live or typ == ISO), int(dbl))
        return ("DATA1" if tog else "DATA0", data)

    def usb_in_done(self, bus, x, acked: bool, t: float) -> None:
        if self.chip is None or not acked:
            return
        n = next((k for k in self._in_pending if self.mem[4 * k] & EA == x.ep), None)
        if n is None:
            return
        _tog, keep_valid, dbl = self._in_pending.pop(n)
        o = 4 * n
        e = self._g(o)
        new = (e | CTR_TX) ^ DTOG_TX
        if not keep_valid:
            new = (new & ~STAT_TX) | (NAK << 4)
        if dbl:
            self._dbl_live.add(n)
        self._s(o, new)
        self.stats["in"] += 1
        self._update_irq()

    # ------------------------------------------------------------------ status
    def usb_stats(self) -> dict:
        return dict(self.stats, address=self._g(OFF_DADDR) & 0x7F, suspended=self._bus_suspended,
                    connected=bool(self.bus is not None and self.bus.state != "detached"))
