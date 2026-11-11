"""Kinetis External Watchdog Monitor (EWM, K22P100M120SF5V2RM ch. 23; chip configuration 3.3.11).

Registers (SVD table): CTRL (+0: INTEN 3, INEN 2, ASSIN 1, EWMEN 0), SERV (+1, write-only, reads 0), CMPL (+2),
CMPH (+3, reset 0xFF).

* Write-once (23.3.1, 23.3.3/4): CTRL.EWMEN/ASSIN/INEN, CMPL and CMPH can be written once after reset. A second write
  is ignored with one `warn` per register (silicon raises a bus transfer error). CTRL.INTEN stays writable.
* Counter: 8-bit, clocked by the 1 kHz LPO (Table 3-28), started at 0 by CTRL.EWMEN=1 (which also deasserts
  EWM_out) and by each legal service. It is computed from virtual time (sched.at_ns, independent of the core clock);
  the only scheduled event is the CMPH expiry.
* Service (23.3.2, 23.4.5): writing 0xB4 then 0x2C to SERV within `service_cycles` bus clocks (15,
  EWM_service_time). Any other value, or the second byte too late, is an illegal service, which has no effect (the
  sequence restarts; 0xB4 starts a new one). A legal service with counter >= CMPL (before expiry) and EWM_in
  deasserted resets the counter. A legal service with counter < CMPL (23.4.1; Table 23-7), or with EWM_in asserted
  (INEN=1), resets the counter and asserts EWM_out. CMPL = 0 (reset value) gives no minimum service time.
* Expiry: counter > CMPH (23.3.4 note: never with CMPH = 0xFF) asserts EWM_out. CMPL == CMPH (illegal) asserts
  it when the counter reaches CMPL + 1 (23.4.4).
* EWM_in (CTRL.INEN=1): the pin muxed to EWM_IN (PTE25/PTB16/PTD4 ALT6) is asserted when its level equals
  CTRL.ASSIN (default active low). An assertion while the EWM is enabled asserts EWM_out (Figure 23-1:
  EWM_out = counter expired | illegal-window service | (EWM_in ^ ~ASSIN) & INEN); X (floating) counts as 0.
* EWM_out is sticky: once asserted only a reset deasserts it (23.4.1). It is low-active (EWM_OUT_b): the signal is
  driven L out of reset (asserted, "external pull-down") and from any assertion, H after EWMEN=1 while
  deasserted, on the pin muxed to EWM_OUT_b (PTE24/PTB17/PTD5 ALT6) via PinBoard.drive_signal.
* Interrupt "WDOG_EWM" (IRQ 22, shared with the WDOG, level): while EWM_out is asserted and CTRL.INTEN=1; clearing
  INTEN deasserts the request without deasserting EWM_out (23.4.6). Every assertion logs one `warn` with the reason.
* `asserted` (bool), `counter()` (current count), `reason` (why EWM_out was asserted) for tests and tools.

Not modelled: Stop/Wait mode counter freeze, debug mode, the service-sequence state across Stop. Clock gate:
SIM_SCGC4[EWM].
"""
from __future__ import annotations

from typing import Any

from ...pins import PinBoard
from ..registry import register
from ._util import GatedBlock, build_regs, irq_number
from .clockutil import bus_hz

INTEN, INEN, ASSIN, EWMEN = 0x08, 0x04, 0x02, 0x01
ONCE_CTRL = INEN | ASSIN | EWMEN
LPO_HZ = 1000.0


@register("kinetis.ewm")
class EWM(GatedBlock):
    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        self.gate = params.get("gate", "EWM")
        ov = {"CTRL": {"ro_mask": 0xF0, "on_write": self._ctrl_write},
              "SERV": {"on_write": self._serv_write, "on_read": lambda b, r: 0},
              "CMPL": {"on_write": self._cmp_write}, "CMPH": {"on_write": self._cmp_write}}
        r = build_regs((name, "EWM"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, r, log=log, params=params)
        self.lpo_hz = float(params.get("lpo_hz", LPO_HZ))
        self.service_cycles = int(params.get("service_cycles", 15))
        self._irqn: int | None = None
        self._in_pins: list = []
        self._init_state()

    def _init_state(self) -> None:
        self.asserted = True                 # EWM_out asserted out of reset until EWMEN=1 (23.4.1)
        self.reason = "reset"
        self.services = 0
        self._written: set[str] = set()
        self._warned: set[str] = set()
        self._t0 = 0.0                        # counter start (ns)
        self._first_ns: float | None = None   # time of the 0xB4 write of a pending service sequence
        self._ev: Any = None
        self._line = False
        self._drive_level: str | None = None

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._irqn = irq_number(chip, "WDOG_EWM", "EWM")
        pins = getattr(chip, "pins", None)
        if pins is not None:
            self._in_pins = pins.signal_pins("EWM_IN")
            for p in self._in_pins:
                pins.input_listeners[p.id].append(self._on_input)

    def reset(self) -> None:
        self._cancel()
        super().reset()
        self._init_state()
        if self.chip is not None:
            if self._irqn is not None:
                self.irq(self._irqn, False)
            self._drive()

    def _cancel(self) -> None:
        if self._ev is not None and self.chip is not None:
            self.chip.sched.cancel(self._ev)
        self._ev = None

    # ------------------------------------------------------------ state
    def enabled(self) -> bool:
        return bool(self.get("CTRL") & EWMEN)

    def counter(self) -> int:
        if not self.enabled() or self.chip is None:
            return 0
        now = self.lp_t0_ns if self.lp_frozen else self.now_ns()
        n = int((now - self._t0) * self.lpo_hz / 1e9 + 1e-6)
        return min(n, 255)

    def _restart(self) -> None:
        self._t0 = self.now_ns()
        self._schedule_expiry()

    def _schedule_expiry(self) -> None:
        self._cancel()
        if self.asserted or not self.enabled():
            return
        cmpl, cmph = self.get("CMPL"), self.get("CMPH")
        if cmpl == cmph:
            limit = cmpl + 1                       # 23.4.4: illegal equal values expire at CMPL + 1
        elif cmph >= 0xFF:
            return                                 # never expires (23.3.4 note)
        else:
            limit = cmph + 1                       # expires when counter > CMPH
        self._ev = self.chip.sched.at_ns(max(self._t0 + limit * 1e9 / self.lpo_hz, self.now_ns()), self._expire)

    def _expire(self) -> None:
        self._ev = None
        self._assert("counter passed CMPH without a service")

    def _input_asserted(self) -> bool:
        if not self.get("CTRL") & INEN:
            return False
        want = "H" if self.get("CTRL") & ASSIN else "L"
        for p in self._in_pins:
            if p.direction == "in" and PinBoard.signal_matches(p.owner, "EWM_IN"):
                lvl = p.level()
                return (lvl if lvl in ("H", "L") else "L") == want
        return False

    def _assert(self, why: str) -> None:
        if self.asserted:
            return
        self.asserted = True
        self.reason = why
        self._cancel()
        self.emit("warn", block=self.name, msg=f"{self.name}: EWM_out asserted ({why}); only a reset deasserts it")
        self._drive()
        self._update_irq()

    def _drive(self) -> None:
        pins = self.chip.pins if self.chip is not None else None
        if pins is None:
            return
        lvl = "L" if self.asserted else "H"
        if lvl != self._drive_level:
            self._drive_level = lvl
            pins.drive_signal("EWM_OUT_b", lvl)

    def _update_irq(self) -> None:
        if self._irqn is None or self.chip is None:
            return
        on = self.asserted and self.enabled() and bool(self.get("CTRL") & INTEN)
        if on != self._line:
            self._line = on
            self.irq(self._irqn, on)

    lp_frozen = False                         # set by the SMC freezer (Table 7-2: EWM static in Stop/VLPx/LLS)

    def time_shift(self, dns: float, dcycles: int) -> None:
        """The EWM counter (LPO, but gated with the bus clock: static in Stop, VLPW, VLPS, LLS) held while asleep."""
        self._t0 += dns
        if self._first_ns is not None:
            self._first_ns += dns
        self._on_input(None)                      # EWM_in asserted while the clock was off: seen now

    def _on_input(self, pin: Any) -> None:
        if self.lp_frozen:
            return                                # static: EWM_in is sampled again after the wake-up
        if self.enabled() and not self.asserted and self._input_asserted():
            self._assert("EWM_in asserted")

    # ------------------------------------------------------------ registers
    def _once(self, reg: Any, old: int, new: int, mask: int) -> int:
        """Write-once bits: the first write after reset sticks; later writes keep the old value."""
        if reg.name in self._written:
            # CTRL: only modifying the write-once bits is an error (INTEN stays writable); CMPL/CMPH: any write
            if (reg.name != "CTRL" or (old ^ new) & mask) and reg.name not in self._warned:
                self._warned.add(reg.name)
                self.emit("warn", block=self.name, msg=f"{self.name}_{reg.name} is write-once after reset; "
                          "second write ignored (real hardware raises a bus transfer error)")
            return (new & ~mask) | (old & mask)
        self._written.add(reg.name)
        return new

    def _ctrl_write(self, blk, reg, old, new):
        new = self._once(reg, old, new, ONCE_CTRL) & 0x0F
        self._store(reg, new)
        if new & EWMEN and not old & EWMEN:
            self.asserted = False                 # enabling deasserts EWM_out and starts the counter at 0
            self.reason = ""
            self._restart()
            self._drive()
            self.emit("info", block=self.name, msg=f"{self.name} enabled: window CMPL={self.get('CMPL')} "
                      f"CMPH={self.get('CMPH')} LPO ticks, EWM_in {'on' if new & INEN else 'off'}")
            if self._input_asserted():
                self._assert("EWM_in asserted")
        self._update_irq()
        return new

    def _cmp_write(self, blk, reg, old, new):
        new = self._once(reg, old, new, 0xFF)
        self._store(reg, new)
        if self.enabled():
            self._schedule_expiry()                # first CMPx write after EWMEN: re-time the expiry
        return new

    def _serv_write(self, blk, reg, old, new):
        v = new & 0xFF
        self._store(reg, 0)
        if not self.enabled():
            return 0
        now = self.now_ns()
        if v == 0xB4:
            self._first_ns = now
            return 0
        first, self._first_ns = self._first_ns, None
        if v != 0x2C or first is None:
            return 0                                          # illegal service: no effect
        if (now - first) * bus_hz(self.chip) / 1e9 > self.service_cycles + 1e-6:
            return 0                                          # second byte too late: illegal, no effect
        if self.asserted:
            return 0
        self.services += 1
        cnt = self.counter()
        if cnt < self.get("CMPL"):
            self._restart()
            self._assert(f"serviced before the window opened (counter {cnt} < CMPL {self.get('CMPL')})")
        elif self._input_asserted():
            self._restart()
            self._assert("serviced while EWM_in is asserted")
        else:
            self._restart()
        return 0
