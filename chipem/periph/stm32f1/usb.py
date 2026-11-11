"""STM32F103 USB full-speed device controller (`stm32f1.usb`, 0x40005C00) and its packet memory (`stm32f1.pma`,
0x40006000), on a chipem.usbbus cable.

Written from RM0008 ch. 23 "Universal serial bus full-speed device interface (USB)" as remembered and from what ST's
USB-FS device library / CubeF1 HAL (`stm32f1xx_ll_usb.c`, `PCD_EP_ISR_Handler`) and libopencm3 `st_usbfs` rely on:
**not verified against RM0008** (ST's server refuses the download).

Packet memory (USB_CAN_SRAM, 512 bytes shared with bxCAN, DS15060 2.3.x): the USB sees 256 16-bit words at local
addresses 0..0x1FF; the CPU (APB1) sees each word in the low half of a 32-bit slot: CPU address = 0x40006000 +
2 * local address.  The upper half of each slot reads 0 and ignores writes; a byte access reaches the low (offset 0)
or high (offset 1) byte of the word.  The memory is not cleared by a reset (SRAM; zero at power-on here).

Registers (16 significant bits at 32-bit stride; bits 31:16 read 0):
  0x00 + 4n EPnR (n = 0..7): CTR_RX (15, rc_w0), DTOG_RX (14, t), STAT_RX (13:12, t), SETUP (11, r), EP_TYPE (10:9),
      EP_KIND (8), CTR_TX (7, rc_w0), DTOG_TX (6, t), STAT_TX (5:4, t), EA (3:0).  rc_w0: writing 0 clears, 1 keeps;
      t (toggle): writing 1 flips, 0 keeps.  A byte write leaves the other byte's bits as "keep" (CTR = 1, t = 0).
  0x40 CNTR (reset 0x0003: FRES | PDWN), 0x44 ISTR (flags 14:8 rc_w0; CTR (15), DIR (4), EP_ID (3:0) read-only,
  computed), 0x48 FNR (read-only: FN, LSOF, LCK, RXDM, RXDP), 0x4C DADDR (EF, ADD), 0x50 BTABLE (15:3).

Buffer descriptor table at BTABLE (packet memory), 8 bytes per endpoint register n: ADDRn_TX, COUNTn_TX, ADDRn_RX,
COUNTn_RX (BL_SIZE (15): 0 = 2-byte blocks, size = 2 * NUM_BLOCK; 1 = 32-byte blocks, size = 32 * (NUM_BLOCK + 1);
NUM_BLOCK (14:10); COUNT (9:0) written by the USB).  Double-buffered bulk (EP_TYPE = bulk, EP_KIND = 1): buffer 0 =
the TX descriptor pair, buffer 1 = the RX pair, for either direction.

Endpoint behaviour (per token, the EPnR whose EA matches the token's endpoint; first match by n):
* SETUP: control endpoints only, accepted whatever STAT_RX is unless DISABLED (USB 2.0 8.5.3: a SETUP is never
  NAKed or STALLed); the 8 bytes go to the RX buffer, COUNT = 8, CTR_RX = SETUP = 1, DTOG_RX = DTOG_TX = 1 (the next
  data/status packets are DATA1), STAT_RX = STAT_TX = NAK until firmware answers.
* OUT: STAT_RX DISABLED -> no answer, STALL -> STALL, NAK -> NAK; VALID: a DATAx PID different from DTOG_RX is a
  retransmission (ACKed, ignored, no CTR); more data than the RX buffer holds -> STALL, no CTR (buffer overrun, as
  ST describes it); a control endpoint with EP_KIND (STATUS_OUT) STALLs an OUT with data.  Else data -> PMA, COUNT,
  CTR_RX = 1, SETUP = 0, DTOG_RX flips, STAT_RX = NAK.
* IN: STAT_TX DISABLED -> no answer, STALL, NAK; VALID: COUNTn_TX bytes from ADDRn_TX with PID DATA<DTOG_TX>, read
  from the PMA at the IN token; on the host's ACK: CTR_TX = 1, DTOG_TX flips, STAT_TX = NAK.
* Double-buffered bulk: the USB uses buffer DTOG (DTOG_RX for OUT, DTOG_TX for IN), the application owns SW_BUF
  (DTOG_TX for OUT, DTOG_RX for IN); DTOG == SW_BUF -> NAK.  STAT stays VALID; the CTR is also reported on the
  high-priority line.  Isochronous endpoints: the usbbus has no isochronous transfers (one `warn`; tokens answered as
  for bulk).
* Address: tokens are answered only while DADDR.EF = 1 and for DADDR.ADD.  A USB reset (and CNTR.FRES 0 -> 1) sets
  ISTR.RESET and clears DADDR and every EPnR (RM0008: "communication is disabled in all endpoint registers"); BTABLE
  and the PMA are kept.
* Bus states: SOF -> ISTR.SOF, FNR.FN, LCK after two consecutive SOFs.  3 ms idle -> ISTR.SUSP (+ ESOF, FNR.LSOF = 3:
  the three missed SOFs reported at once, not 1 ms apart).  Host resume or reset while suspended -> ISTR.WKUP, CNTR.
  LP_MODE cleared, EXTI line 18 (USBWakeUp) pulsed.  CNTR.RESUME 0 -> 1 while the bus is suspended = remote wakeup
  (bus.remote_wakeup()).  FNR.RXDP/RXDM: J (10) on an idle bus, SE0 in reset, K while resuming.
* IRQs: USB_LP_CAN1_RX0 = (CTRM and any CTR) or (ISTR[14:8] & CNTR[14:8]); USB_HP_CAN1_TX = CTRM and a CTR on an
  isochronous or double-buffered bulk endpoint.  Both vectors are shared with bxCAN (`shared_irq` "USB" share).
  ERR / PMAOVR never occur (error-free cable, PMA arbitration not modelled).

Connection (the F103 has no internal D+ pull-up: boards fit 1.5 k to 3.3 V, fixed or switched): the device is
attached when the transceiver is on (RCC_APB1ENR.USBEN and CNTR.PDWN = 0), CNTR.FRES = 0, USBCLK is 48 MHz +-0.25 %
and the pull-up is present: param `pullup` = "always" (default, a fixed resistor as on the "blue pill"), "none", or
a pin name ("PA8": high connects; "!PA8": low connects, e.g. a PNP switch).  USBCLK not 48 MHz: one `warn`, no
connection (silicon would not work).  `transceiver_on()` gives PA11/PA12 to USBDM/USBDP (stm32f1.gpio).
USB and bxCAN share the 512-byte SRAM: when the USB becomes active while the CAN clock is on and the CAN block reports
`active()`, one `warn` (the PMA content is then undefined on silicon; here both just use it).
RCC_APB1RSTR.USBRST resets the block (registers; the PMA keeps its content).
"""
from __future__ import annotations

from typing import Any

from ..base import MMIOBlock
from ..registry import register
from ._util import GatedBlock, build_regs, find_model, fmt_hz, gpio_ports, irq_number, rcc, shared_irq
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
        super().reset()
        self._in_pending.clear()
        self._bus_suspended = False
        self._sofs = 0
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
        if new & RESUME and not old & RESUME and self._bus_suspended and self.bus is not None:
            self.bus.remote_wakeup()
        if (old ^ new) & (FRES | PDWN):
            self._connection()
        self._update_irq()
        return None

    def _istr_read(self, blk, reg) -> int:
        v = self._g(OFF_ISTR) & ISTR_FLAGS
        m = self.mem
        for n in range(NEP):
            e = m[4 * n] | m[4 * n + 1] << 8
            if e & EP_RC:
                v |= I_CTR | (e & EA)
                if e & CTR_RX:
                    v |= 0x10
                break
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
        """USB reset / FRES: DADDR and every EPnR cleared, FNR lock lost."""
        for n in range(NEP):
            self._s(4 * n, 0)
        self._s(OFF_DADDR, 0)
        self._s(OFF_FNR, self._g(OFF_FNR) & 0x7FF)
        self._sofs = 0
        self._in_pending.clear()

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
        self._s(OFF_CNTR, self._g(OFF_CNTR) & ~LP_MODE)
        self._flag(I_WKUP)
        _util.exti_line(self.chip, 18, True)
        _util.exti_line(self.chip, 18, False)

    def usb_reset(self, bus, active: bool, t: float) -> None:
        if not active:
            return
        was = self._bus_suspended
        self._bus_suspended = False
        if not self._sie_runs():
            return
        if was:
            self._wake()
        self.stats["resets"] += 1
        self._usb_reset_state()
        self._flag(I_RESET)

    def usb_suspend(self, bus, t: float) -> None:
        self._bus_suspended = True
        if self._sie_runs():
            self._sofs = 0
            self._s(OFF_FNR, (self._g(OFF_FNR) & 0x7FF) | (3 << 11))
            self._flag(I_SUSP | I_ESOF)

    def usb_resume(self, bus, t: float) -> None:
        if not self._bus_suspended:
            return
        self._bus_suspended = False
        if self._sie_runs():
            self._wake()

    def usb_sof(self, bus, frame: int, t: float) -> None:
        if not self._sie_runs():
            return
        self.stats["sof"] += 1
        self._sofs += 1
        self._s(OFF_FNR, (frame & 0x7FF) | (LCK if self._sofs >= 2 else 0))
        self._flag(I_SOF)

    def _match(self, x) -> int | None:
        if self.chip is None or getattr(self, "lp_frozen", False) or not self._sie_runs():
            return None                                      # also: USBEN off, powered down, FRES, no 48 MHz
        da =self._g(OFF_DADDR)
        if not da & 0x80 or x.addr != da & 0x7F:
            return None
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
        addr, cnt = self._buf(n, 1)
        pma = self.pma
        size = self._rx_size(pma.r16(cnt))
        data = bytes(x.data)
        if len(data) > size:
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
        if dbl and tog == (1 if e & DTOG_TX else 0):
            return self._nak()                               # both buffers held by the application
        if typ != ISO and got != tog:
            return "ACK"                                     # retransmission after a lost ACK: ignored
        addr, cnt = self._buf(n, tog if dbl else 1)
        pma = self.pma
        size = self._rx_size(pma.r16(cnt))
        if len(data) > size:
            self._warn_once(f"ovr{n}", f"{self.name}: EP{n}R OUT packet of {len(data)} bytes, RX buffer holds "
                            f"{size}: buffer overrun, STALL")
            return self._stall()
        pma.write_bytes(addr, data)
        pma.w16(cnt, (pma.r16(cnt) & 0xFC00) | len(data))
        new = (e & ~SETUP) | CTR_RX
        new ^= DTOG_RX
        if not dbl and typ != ISO:
            new = (new & ~STAT_RX) | (NAK << 12)
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
        tog = 1 if e & DTOG_TX else 0
        if dbl and tog == (1 if e & DTOG_RX else 0):
            return self._nak()                               # no buffer filled by the application
        addr, cnt = self._buf(n, tog if dbl else 0)
        pma = self.pma
        count = pma.r16(cnt) & 0x3FF
        data = pma.read_bytes(addr, count)
        self._in_pending[n] = (tog, int(dbl or typ == ISO))
        return ("DATA1" if tog else "DATA0", data)

    def usb_in_done(self, bus, x, acked: bool, t: float) -> None:
        if self.chip is None or not acked:
            return
        n = next((k for k in self._in_pending if self.mem[4 * k] & EA == x.ep), None)
        if n is None:
            return
        _tog, keep_valid = self._in_pending.pop(n)
        o = 4 * n
        e = self._g(o)
        new = (e | CTR_TX) ^ DTOG_TX
        if not keep_valid:
            new = (new & ~STAT_TX) | (NAK << 4)
        self._s(o, new)
        self.stats["in"] += 1
        self._update_irq()

    # ------------------------------------------------------------------ status
    def usb_stats(self) -> dict:
        return dict(self.stats, address=self._g(OFF_DADDR) & 0x7F, suspended=self._bus_suspended,
                    connected=bool(self.bus is not None and self.bus.state != "detached"))
