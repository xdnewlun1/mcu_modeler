"""Kinetis K22 USB Device Charger Detection module (USBDCD, K22P100M120SF5V2RM ch. 44; `kinetis.usbdcd`, 0x40035000).

Registers (SVD table USBDCD, reset values from it): 0x00 CONTROL (IACK w1, IF ro, IE, BC12, START w1, SR w1),
0x04 CLOCK (CLOCK_UNIT, CLOCK_SPEED), 0x08 STATUS (SEQ_RES, SEQ_STAT, ERR, TO, ACTIVE; read-only), 0x10 TIMER0
(TSEQ_INIT, TUNITCON live), 0x14 TIMER1 (TVDPSRC_ON, TDCD_DBNC), 0x18 TIMER2_BC11 (CHECK_DM, TVDPSRC_CON) /
TIMER2_BC12 (TVDMSRC_ON, TWAIT_AFTER_PRD) - one register, read per CONTROL.BC12.

The charger detection sequence (44.5.1) runs on scheduler events in the chip's time domain, cancelled on reset / SR:
* START (ignored while ACTIVE): ACTIVE = 1, the unit connection timer TUNITCON starts at TSEQ_INIT and counts ms
  (saturating at 4095).
* Data pin contact (44.5.1.3): the D+ line goes low when the data pins touch a port (a host's 15 k pull-downs): here
  when the USB0 port is plugged into a powered cable (a UsbBus with VBUS on).  After TDCD_DBNC ms of contact:
  SEQ_STAT = 01 (no interrupt).  Contact lost during the debounce restarts it.
* Charging port detection (44.5.1.4): 1 ms, then TVDPSRC_ON ms later D- is sampled.  The emulated host is a standard
  downstream port (D- stays below VDAT_REF): SEQ_RES = 01, SEQ_STAT = 10, CONTROL.IF = 1 and the sequence ends
  (ACTIVE = 0, TUNITCON stops).  Charging / dedicated charging ports are not modelled.
* Timeout (44.5.1.6): TUNITCON reaching 1000 ms while ACTIVE: TO = 1, ERR = 1, IF = 1; the sequence keeps running
  until SR (e.g. no cable: the data pins never make contact).
* SR: software reset (Table 44-22): IF, STATUS and TUNITCON cleared, the sequence stops; CLOCK / timers / IE kept.
  IACK clears IF.  IRQ "USBDCD" (by name) = IF & IE.
* Timing: the module counts ms from its clock (the bus clock, RM 5.7) using CLOCK_SPEED / CLOCK_UNIT; when the
  programmed value differs from the real bus clock, every interval scales accordingly (one `info` says so).
Clock gate SIM_SCGC6[USBDCD] (one `warn` per access with the gate off).
"""
from __future__ import annotations

from typing import Any

from ..registry import register
from ._util import GatedBlock, build_regs, find_model, fmt_hz, irq_number
from .clockutil import bus_hz

IACK, IF, IE, BC12, START, SR = 1, 1 << 8, 1 << 16, 1 << 17, 1 << 24, 1 << 25
ACTIVE, TO, ERR = 1 << 22, 1 << 21, 1 << 20


@register("kinetis.usbdcd")
class UsbDcd(GatedBlock):
    gate = "USBDCD"

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        ov = {
            "CONTROL": {"ro_mask": 0xFCFCFEFE | IF, "on_write": self._control_write},
            "CLOCK": {"ro_mask": 0xFFFFF002},
            "STATUS": {"ro_mask": 0xFFFFFFFF},
            "TIMER0": {"ro_mask": 0xFC00FFFF, "on_read": self._timer0_read},
            "TIMER1": {"ro_mask": 0xFC00FC00},
            "TIMER2_BC11": {"ro_mask": 0xFC00FC00},
        }
        r = build_regs((name, "USBDCD"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self._irqn: int | None = None
        self._usb: Any = None
        self._ev = None
        self._to_ev = None
        self._t0 = 0.0
        self._unit = 1e6
        self._tunitcon: int | None = 0        # frozen value while not counting
        self._phase = None                    # None | "contact" | "debounce" | "primary"
        self._line = False
        self._noted: set[str] = set()

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irqn = irq_number(chip, "USBDCD", self.name)
        self._usb = find_model(chip, "kinetis.usbfs", "USB0")
        if self._usb is not None:
            self._usb.vbus_listeners.append(self._vbus_changed)

    # ------------------------------------------------------------------ helpers
    def _cancel(self) -> None:
        sched = getattr(self.chip, "sched", None)
        for nm in ("_ev", "_to_ev"):
            ev = getattr(self, nm)
            if ev is not None and sched is not None:
                sched.cancel(ev)
            setattr(self, nm, None)

    def reset(self) -> None:
        self._cancel()
        super().reset()
        self._phase = None
        self._tunitcon = 0
        if self.chip is not None:
            self._update_irq()

    def _update_irq(self) -> None:
        if self.chip is None or self._irqn is None:
            return
        c = self.get("CONTROL")
        line = bool(c & IF and c & IE)
        if line != self._line:
            self._line = line
            self.irq(self._irqn, line)

    def _ms_ns(self) -> float:
        """Length of one module 'ms': CLOCK_SPEED tells the module its clock; the real clock is the bus clock."""
        clk = self.get("CLOCK")
        prog = ((clk >> 2) & 0x3FF) * (1e6 if clk & 1 else 1e3)
        real = bus_hz(self.chip)
        if prog <= 0 or real <= 0:
            return 1e6
        if abs(prog - real) > 0.01 * real:
            key = f"{round(prog)}/{round(real)}"
            if key not in self._noted:
                self._noted.add(key)
                self.emit("info", block=self.name, msg=f"{self.name}: CLOCK says {fmt_hz(prog)} but the bus clock "
                          f"is {fmt_hz(real)}: the charger detection timings scale by {prog / real:.3f}")
        return 1e6 * prog / real

    def _field(self, reg: str, lsb: int, width: int) -> int:
        return (self.get(reg) >> lsb) & ((1 << width) - 1)

    def time_shift(self, dns: float, dcycles: int) -> None:
        """Stop/VLPS/LLS (Table 7-2: USB DCD static): the sequence timers held while the bus clock was off."""
        self._t0 += dns

    def _tunit_now(self) -> int:
        if self._tunitcon is not None:
            return self._tunitcon
        init = self._field("TIMER0", 16, 10)
        now = self.lp_t0_ns if getattr(self, "lp_frozen", False) else self.chip.now_ns()
        el = int((now - self._t0) // self._unit)
        return min(0xFFF, init + max(0, el))

    def _timer0_read(self, blk, reg) -> int:
        return (self.get("TIMER0") & ~0xFFF) | self._tunit_now()

    def _status(self, set_bits: int = 0, clear_bits: int = 0) -> None:
        self.set("STATUS", (self.get("STATUS") & ~clear_bits) | set_bits)

    def _interrupt(self) -> None:
        self.set("CONTROL", self.get("CONTROL") | IF)
        self._update_irq()

    def _contact(self) -> bool:
        u = self._usb
        b = getattr(u, "bus", None) if u is not None else None
        return bool(b is not None and b.vbus)

    # ------------------------------------------------------------------ control
    def _control_write(self, blk, reg, old, new):
        v = new & ~(IACK | START | SR)
        if self.get("STATUS") & ACTIVE:
            v = (v & ~BC12) | (old & BC12)                  # 44.4.1: BC12 cannot change after start
        if new & IACK:
            v &= ~IF
        if new & SR:
            self._cancel()
            self._phase = None
            self._tunitcon = 0
            v &= ~IF
            self.set("STATUS", 0)
        self.set("CONTROL", v)
        if new & START and not self.get("STATUS") & ACTIVE and self.chip is not None:
            self._start()
        self._update_irq()
        return v

    def _start(self) -> None:
        self._unit = self._ms_ns()
        self._t0 = self.chip.now_ns()
        self._tunitcon = None
        self._status(ACTIVE)
        init = self._field("TIMER0", 16, 10)
        self._to_ev = self.chip.sched.at_ns(self._t0 + max(0, 1000 - init) * self._unit, self._timeout)
        self._phase = "contact"
        if self._contact():
            self._debounce()

    def _after(self, ms: float, cb) -> None:
        if self._ev is not None:
            self.chip.sched.cancel(self._ev)
        self._ev = self.chip.sched.in_ns(ms * self._unit, cb)

    def _vbus_changed(self, on: bool) -> None:
        if self._phase == "contact" and on:
            self._debounce()
        elif self._phase == "debounce" and not on:
            if self._ev is not None:
                self.chip.sched.cancel(self._ev)
                self._ev = None
            self._phase = "contact"

    def _debounce(self) -> None:
        self._phase = "debounce"
        self._after(self._field("TIMER1", 16, 10), self._contact_done)

    def _contact_done(self) -> None:
        self._ev = None
        self._status(1 << 18, 3 << 18)                      # SEQ_STAT = 01: data pin contact (no interrupt)
        self._phase = "primary"
        self._after(1 + self._field("TIMER1", 0, 10), self._primary_done)

    def _primary_done(self) -> None:
        self._ev = None
        # D- sampled below VDAT_REF: standard downstream port (the emulated host); the sequence ends.
        self._tunitcon = self._tunit_now()
        if self._to_ev is not None:
            self.chip.sched.cancel(self._to_ev)
            self._to_ev = None
        self._phase = None
        st = self.get("STATUS") & ~(ACTIVE | (0xF << 16))
        self.set("STATUS", st | (2 << 18) | (1 << 16))
        self._interrupt()

    def _timeout(self) -> None:
        self._to_ev = None
        if not self.get("STATUS") & ACTIVE:
            return
        self._status(TO | ERR)
        self._interrupt()
