"""Shared helpers for the TI C2000 models: register lists from the hand-written register table
(chips/tms320f28232_regs.yaml, 16-bit units), peer lookup, PIE line names, EALLOW protection, clock gates."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from ...regtable import regs_for
from ..base import MMIOBlock, Reg

_PARKED = 1 << 62                 # cycle stamp of a scheduler event parked while its block's clock is stopped
DEFAULT_REGS = Path(__file__).resolve().parents[3] / "chips" / "tms320f28232_regs.yaml"
ST1_EALLOW = 1 << 6


def table(names: Iterable[str], regs_file: str | None = None):
    for n in names:
        if not n:
            continue
        pr = regs_for(n, regs_file or DEFAULT_REGS)
        if pr is not None:
            return pr
    return None


def build_regs(names: Iterable[str], size: int, overrides: dict[str, dict] | None = None,
               regs_file: str | None = None, unit_bits: int = 16) -> list[Reg]:
    """Reg descriptors (16-bit units) from the register table entry of the first of `names`, with per-register
    keyword overrides (on_read, on_write, ro_mask, w1c_mask, reset).  The table's masks are unit-aware
    (`regtable.Register.ro_mask` spans width x 16 bits)."""
    names = list(names)
    pr = table(names, regs_file)
    if pr is None:
        raise KeyError(f"no register table entry for any of {names}")
    overrides = overrides or {}
    out: list[Reg] = []
    used: set[int] = set()
    for spec in pr.reg_specs(unit_bits):
        spec.update(overrides.get(spec["name"], {}))
        off, w = spec["offset"], spec["width"]
        if off + w > size or any(i in used for i in range(off, off + w)):
            continue
        used.update(range(off, off + w))
        out.append(Reg(**spec))
    return out


def find_model(chip: Any, model: str, name: str | None = None) -> MMIOBlock | None:
    if chip is None:
        return None
    periphs = getattr(chip, "peripherals", {}) or {}
    if name is not None:
        b = periphs.get(name)
        if b is not None and getattr(b, "model", None) == model:
            return b
    for b in periphs.values():
        if getattr(b, "model", None) == model:
            return b
    return None


def sysctrl(chip: Any):
    return find_model(chip, "ti_c2000.sysctrl", "SYSCTRL")


def sysclk_hz(chip: Any) -> float:
    return float(chip.clock.hz) if chip is not None else 0.0


def lspclk_hz(chip: Any) -> float:
    sc = sysctrl(chip)
    if sc is not None:
        return sc.lspclk_hz()
    return sysclk_hz(chip) / 4.0


def hspclk_hz(chip: Any) -> float:
    sc = sysctrl(chip)
    if sc is not None:
        return sc.hspclk_hz()
    return sysclk_hz(chip) / 2.0


def fmt_hz(hz: float) -> str:
    if hz >= 1e6:
        return f"{hz / 1e6:.3f} MHz"
    if hz >= 1e3:
        return f"{hz / 1e3:.3f} kHz"
    return f"{hz:.0f} Hz"


def irq_line(chip: Any, name: str, default: str | None = None):
    """PIE line for an interrupt name from the chip YAML (`irqs: {vector: name}`), else `default` ("x.y")."""
    desc = getattr(chip, "desc", None)
    for n, nm in (getattr(desc, "irqs", None) or {}).items():
        if nm == name:
            return n
    return default


class C2000Block(MMIOBlock):
    """Base for C2000 models: 16-bit units, EALLOW write protection and a PCLKCRx clock gate.

    `eallow_regs`: names of EALLOW-protected registers (None = none, "*" = all).  A write to one while ST1.EALLOW
    is clear is ignored, as on silicon, and logged once per block as a `warn`.  `gate`: PCLKCRx bit name
    (e.g. "SCIAENCLK"); an access with the clock off logs one `warn` and proceeds."""

    unit_bits = 16
    eallow_regs: Any = None
    gate: str | None = None
    # attributes holding absolute core-cycle anchors of time-derived counters (shifted by the stopped time when the
    # clock resumes after STANDBY/HALT, so the counters do not advance while SYSCLKOUT is off)
    cycle_anchors: tuple[str, ...] = ()
    clk_stopped = False                 # SYSCLKOUT to this block is off (STANDBY / HALT): pin inputs are ignored

    def __init__(self, name, base, size, regs=(), *, params=None, log=None, ram_like=True):
        super().__init__(name, base, size, regs, ram_like=ram_like, log=log, params=params, unit_bits=16)
        self.enforce_eallow = bool(self.params.get("enforce_eallow", True))
        self._eallow_warned = False
        self._gate_warned = False
        self._sysctrl = None
        prot = self.eallow_regs
        if prot == "*":
            prot = set(self.regs)
        self._protected = {self.regs[n].offset for n in (prot or ()) if n in self.regs}
        if self._protected:
            for n in (prot or ()):
                r = self.regs.get(n)
                if r is not None:
                    self._protected.update(range(r.offset, r.offset + r.width))

    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._sysctrl = sysctrl(chip)

    def eallow(self) -> bool:
        """True if EALLOW is set in the core's ST1 (or the core cannot tell us)."""
        core = getattr(self.chip, "core", None)
        if core is None:
            return True
        try:
            return bool(core.read_reg("st1") & ST1_EALLOW)
        except Exception:
            return True

    def check_gate(self) -> None:
        if self.gate is None or self._gate_warned:
            return
        sc = self._sysctrl
        if sc is not None and not sc.clock_enabled(self.gate):
            self._gate_warned = True
            self.emit("warn", block=self.name, msg=f"{self.name} accessed with its clock disabled "
                      f"(PCLKCR {self.gate}=0); real silicon ignores the access")

    # ------------------------------------------------------------------ low-power clock stop (CONTRACTS "Low-power")
    def clock_stop(self, stopped: bool, mode: str = "") -> None:
        """SYSCLKOUT to this block stops (STANDBY/HALT) or resumes.  Generic freeze: this block's pending scheduler
        events (bound methods of the block) are parked and re-armed after the same remaining number of cycles, and
        the `cycle_anchors` are moved forward by the stopped time, so counters and timing resume where they were."""
        chip = self.chip
        if chip is None or stopped == self.clk_stopped:
            return
        sched = chip.sched
        if stopped:
            self.clk_stopped = True
            now = chip.clock.cycles
            self._stop_cycle = now
            parked = []
            from ..lowpower import event_owner
            for ev in sched._heap:
                if ev.cancelled or event_owner(ev) is not self:      # bound methods of the block or of its
                    continue                                          # helpers (`lp_owner`: I2C master, ...)
                parked.append((ev, ev.cycles - now, ev.t_ns is not None))
                ev.cycles = _PARKED
                ev.t_ns = None
            self._parked = parked
            if parked:
                import heapq
                heapq.heapify(sched._heap)
            return
        self.clk_stopped = False
        now = chip.clock.cycles
        dt = now - getattr(self, "_stop_cycle", now)
        for ev, rem, wall in getattr(self, "_parked", ()):
            ev.cycles = now + max(rem, 0)
            if wall:
                ev.t_ns = chip.clock.cycles_to_ns(ev.cycles)
        if getattr(self, "_parked", None):
            import heapq
            heapq.heapify(sched._heap)
        self._parked = []
        if dt:
            for a in self.cycle_anchors:
                v = getattr(self, a, None)
                if isinstance(v, int):
                    setattr(self, a, v + dt)
                elif isinstance(v, dict):
                    for k in list(v):
                        if isinstance(v[k], int):
                            v[k] += dt
            self.clock_resumed(dt)

    def reset(self) -> None:
        self.clk_stopped = False            # a reset (XRS, watchdog, missing clock) restarts every clock
        self._parked = []
        super().reset()

    def clock_resumed(self, dt_cycles: int) -> None:
        """Hook for models with state the generic freeze does not cover (called after a STANDBY/HALT stop)."""

    def write(self, offset: int, size: int, value: int) -> None:
        if self._protected and offset in self._protected and self.enforce_eallow and not self.eallow():
            if not self._eallow_warned:
                self._eallow_warned = True
                r = self.reg_at(offset)
                self.emit("warn", block=self.name, msg=f"{self.name}.{r.name if r else hex(offset)}: write ignored, "
                          "register is EALLOW-protected and EALLOW is not set")
            return
        if not self._gate_warned and self.gate is not None:
            self.check_gate()
        MMIOBlock.write(self, offset, size, value)


def gpio_resync(chip: Any, sig: str) -> None:
    """A block changed the live direction / drive of its pin signal `sig` (`signal_state`): let the GPIO mux model
    re-evaluate the pins muxed to it."""
    g = find_model(chip, "ti_c2000.gpio", "GPIO")
    if g is not None:
        g.resync_signal(sig)


def listen_input(chip: Any, pin: Any, cb) -> None:
    """Subscribe a peripheral input to `pin` (cb(pin) on a level change).  Through the GPIO model when it owns the pin,
    so the change arrives after the pin's input qualification (GPxQSEL / GPxCTRL, SPRUI07 1.4.4: qualification sits
    in front of the mux and applies to peripheral inputs too); else straight from the pin board."""
    g = find_model(chip, "ti_c2000.gpio", "GPIO")
    if g is not None and g.owns_pin(pin):
        g.periph_listeners.setdefault(pin.id, []).append(cb)
    else:
        chip.pins.input_listeners[pin.id].append(cb)


def unlisten_input(chip: Any, pin: Any, cb) -> None:
    g = find_model(chip, "ti_c2000.gpio", "GPIO")
    for lst in ((g.periph_listeners.get(pin.id) if g is not None else None),
                chip.pins.input_listeners.get(pin.id)):
        if lst is not None and cb in lst:
            lst.remove(cb)
