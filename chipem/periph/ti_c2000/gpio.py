"""C2833x GPIO (0x6F80): ports A (GPIO0-31), B (GPIO32-63), C (GPIO64-87); all registers 32-bit (two words, low word
at the lower address), 16- and 32-bit accesses allowed.

* GPxMUX1/2: 2 bits per pin. 0 = GPIO, 1..3 = the pin's `MUX1`..`MUX3` function from the chip YAML (port C: MUX1 is
  still the GPIO, MUX2/MUX3 the XINTF signal). The pin's owner on the PinBoard follows; direction and idle drive of
  a peripheral signal come from the C2000 signal table (`signals.py`: SCITXD -> output idling H, SCIRXD -> input,
  SDAA/SCLA -> open-drain, unknown -> "none"). A block with mode-dependent pins (McBSP, SPI) overrides the static
  role live through `owned_signals()` / `signal_state(sig)` (and calls `_util.gpio_resync` when it changes); blocks
  that drive their signals edge by edge use PinBoard.drive_signal.
* GPxDIR: 1 = output.  GPxPUD: 1 = pull-up disabled (reset: GPIO0-11 disabled, GPIO12-87 enabled).
* GPxDAT read = resolved pin levels (H → 1; L/X → 0, a floating read logs one `warn` per pin); write = output latch.
* Input qualification (SPRUI07 1.4.4, ports A/B): GPxQSEL = 1/2 (3/6 samples) with the sampling period from
  GPxCTRL.QUALPRDn (2 x QUALPRD SYSCLKOUT, 1 if 0): a pin change reaches GPxDAT and XINT only after it has been
  sampled 3/6 times in a row (first sample clock edge after the change + 2/5 periods); shorter glitches are filtered.
  Synchronous (0) and asynchronous (3) inputs pass at once (the one-SYSCLKOUT synchronisation delay is not
  modelled). Peripheral inputs (eCAP, eQEP, TZ, SCI, SPI, McBSP, ...) see the raw pin.
  GPxSET/CLEAR/TOGGLE: writing 1 sets/clears/toggles latch bits; they read 0.  Outputs drive the latch value when the
  pin is muxed to GPIO with DIR = 1.  DAT reads are cached and invalidated by input changes and configuration writes.
* GPIOXINT1SEL/2SEL/XNMISEL pick GPIO0-31, GPIOXINT3..7SEL GPIO32-63 (value + 32) as external interrupt sources;
  the `ti_c2000.xint` model asks `xint_pin(n)`.
* Control and interrupt-select registers are EALLOW protected; the data registers are not.
Pin discovery (chipem.pinreport): configuration changes are reported with the writing PC; data writes/reads bump
counters returned by `pin_stats()`.
"""
from __future__ import annotations

import re
from typing import Any

from ..registry import register
from ._util import C2000Block, build_regs, find_model
from .signals import TABLE as SIGNALS

PORTS = ("A", "B", "C")
_PORT_NPINS = {"A": 32, "B": 32, "C": 24}
_CTRL_BASE = {"A": 0x00, "B": 0x10, "C": 0x20}
_DATA_BASE = {"A": 0x40, "B": 0x48, "C": 0x50}
_DATA_OFF = {0x40: "A", 0x48: "B", 0x50: "C"}
_EPWM_SIG = re.compile(r"^EPWM(\d+)([AB])$")


def _zero(blk, reg):
    return 0


@register("ti_c2000.gpio")
class GPIO(C2000Block):
    eallow_regs = (["GPACTRL", "GPAQSEL1", "GPAQSEL2", "GPAMUX1", "GPAMUX2", "GPADIR", "GPAPUD",
                    "GPBCTRL", "GPBQSEL1", "GPBQSEL2", "GPBMUX1", "GPBMUX2", "GPBDIR", "GPBPUD",
                    "GPCMUX1", "GPCMUX2", "GPCDIR", "GPCPUD", "GPIOLPMSEL"]
                   + ["GPIOXINT1SEL", "GPIOXINT2SEL", "GPIOXNMISEL"] + [f"GPIOXINT{i}SEL" for i in range(3, 8)])

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        params = dict(params or {})
        ov: dict[str, dict] = {}
        for p in PORTS:
            ov[f"GP{p}MUX1"] = {"on_write": self._cfg_write}
            ov[f"GP{p}MUX2"] = {"on_write": self._cfg_write}
            ov[f"GP{p}DIR"] = {"on_write": self._cfg_write}
            ov[f"GP{p}PUD"] = {"on_write": self._cfg_write}
            ov[f"GP{p}DAT"] = {"on_write": self._dat_write, "on_read": self._dat_read}
            ov[f"GP{p}SET"] = {"on_write": self._set_write, "on_read": _zero}
            ov[f"GP{p}CLEAR"] = {"on_write": self._clear_write, "on_read": _zero}
            ov[f"GP{p}TOGGLE"] = {"on_write": self._toggle_write, "on_read": _zero}
        for n in ("GPIOXINT1SEL", "GPIOXINT2SEL", "GPIOXNMISEL", *(f"GPIOXINT{i}SEL" for i in range(3, 8))):
            ov[n] = {"on_write": self._sel_write}
        for p in ("A", "B"):
            for n in (f"GP{p}CTRL", f"GP{p}QSEL1", f"GP{p}QSEL2"):
                ov[n] = {"on_write": self._qual_write}
        regs = build_regs((name, "GPIO"), size, ov, params.get("regs_file"))
        super().__init__(name, base, size, regs, params=params, log=log)
        self.pins: dict[str, list[Any]] = {p: [None] * 32 for p in PORTS}
        self.latch = {p: 0 for p in PORTS}
        self._dat: dict[str, int | None] = {p: None for p in PORTS}
        self._warned_x: set[str] = set()
        self._rep: Any = None
        self._xint: Any = None
        self._sig_owners: dict[str, Any] | None = None     # signal name -> block with signal_state (lazy)
        # input qualification (ports A/B, GPxQSEL = 3 or 6 samples): pin id -> qualified level / pending event
        self._qlvl: dict[str, str] = {}
        self._qev: dict[str, Any] = {}
        self._pos: dict[str, tuple[str, int]] = {}
        self.qual_listeners: dict[str, list] = {}          # pin id -> [cb(pin, qualified level)] (XINT)
        # pin-discovery counters (kept across resets)
        self.n_reads = {p: 0 for p in PORTS}
        self.writes = {p: [0] * 32 for p in PORTS}
        self.changes = {p: [0] * 32 for p in PORTS}
        self.x_read = {p: 0 for p in PORTS}

    # ------------------------------------------------------------------ setup
    def attach(self, chip: Any) -> None:
        super().attach(chip)
        self._sig_owners = None
        board = getattr(chip, "pins", None)
        if board is not None:
            for p in PORTS:
                for i in range(_PORT_NPINS[p]):
                    pin = board.gpio(p, i)
                    self.pins[p][i] = pin
                    if pin is not None:
                        self._pos[pin.id] = (p, i)
                        board.input_listeners[pin.id].append(self._make_invalidator(p))
            try:
                from ...pinreport import ensure
                self._rep = ensure(chip)
                if self._rep is not None:
                    self._rep.hooked = True
            except Exception:
                self._rep = None
        self._xint = find_model(chip, "ti_c2000.xint")

    def _make_invalidator(self, port: str):
        def inv(pin: Any = None, _p=port) -> None:
            if pin is not None and pin.id in self._qlvl:
                self._qual_change(pin)
                return
            self._dat[_p] = None
            if pin is not None:
                cbs = self.qual_listeners.get(pin.id)
                if cbs:
                    lvl = pin.level()
                    for cb in list(cbs):
                        cb(pin, lvl)
        return inv

    # ------------------------------------------------------------------ input qualification (SPRUI07 1.4.4)
    def qual_mode(self, port: str, i: int) -> tuple[int, int] | None:
        """(samples, sampling period in SYSCLKOUT cycles) of a pin in sampling-window mode, else None (synchronous /
        asynchronous: the change is seen at once)."""
        if port not in ("A", "B"):
            return None
        q = (self.get(f"GP{port}QSEL{1 if i < 16 else 2}") >> (2 * (i & 15))) & 3
        if q not in (1, 2):
            return None
        prd = (self.get(f"GP{port}CTRL") >> (8 * (i // 8))) & 0xFF
        return (3 if q == 1 else 6), (2 * prd if prd else 1)

    def qlevel(self, pin: Any) -> str:
        """Level of `pin` after input qualification (what GPxDAT and XINT see)."""
        return self._qlvl.get(pin.id) or pin.level()

    def _qual_change(self, pin: Any) -> None:
        """A qualified pin changed: the new level passes once it has been sampled `samples` times in a row, i.e. at
        the first sampling instant after the change (sample clock aligned to SYSCLKOUT / (2 QUALPRD)) plus
        (samples - 1) sampling periods; a change back before then is a glitch and is filtered out."""
        ev = self._qev.pop(pin.id, None)
        if ev is not None:
            self.chip.sched.cancel(ev)
        lvl = pin.level()
        if lvl == self._qlvl.get(pin.id):
            return
        port, i = self._pos[pin.id]
        n, ts = self.qual_mode(port, i) or (3, 1)
        now = self.chip.now_cycles()
        first = -(-now // ts) * ts
        if first == now:
            first += ts
        self._qev[pin.id] = self.chip.sched.at_cycles(first + (n - 1) * ts, self._qual_commit, pin, lvl)

    def _qual_commit(self, pin: Any, lvl: str) -> None:
        self._qev.pop(pin.id, None)
        if pin.id not in self._qlvl or pin.level() != lvl:
            return
        self._qlvl[pin.id] = lvl
        self._dat[self._pos[pin.id][0]] = None
        for cb in list(self.qual_listeners.get(pin.id) or ()):
            cb(pin, lvl)

    def _qual_write(self, blk, reg, old, new):
        self._store(reg, new)
        self._qual_sync(reg.name[2])
        return None

    def _qual_sync(self, port: str) -> None:
        """Pins entering sampling-window mode start from their current level; pins leaving it see the pin at once."""
        for i, pin in enumerate(self.pins[port]):
            if pin is None:
                continue
            q = self.qual_mode(port, i)
            if q is not None and pin.id not in self._qlvl:
                self._qlvl[pin.id] = pin.level()
            elif q is None and pin.id in self._qlvl:
                old = self._qlvl.pop(pin.id)
                ev = self._qev.pop(pin.id, None)
                if ev is not None:
                    self.chip.sched.cancel(ev)
                lvl = pin.level()
                if lvl != old:
                    for cb in list(self.qual_listeners.get(pin.id) or ()):
                        cb(pin, lvl)
        self._dat[port] = None

    def reset(self) -> None:
        if self.chip is not None:
            for ev in self._qev.values():
                self.chip.sched.cancel(ev)
        self._qev.clear()
        self._qlvl.clear()
        super().reset()
        for p in PORTS:
            self.latch[p] = 0
            self._dat[p] = None
        self._warned_x.clear()
        if self.chip is not None and self.chip.pins is not None:
            for p in PORTS:
                for i in range(_PORT_NPINS[p]):
                    self._sync_pin(p, i, quiet=True)
        if self._xint is not None:
            self._xint.sources_changed()

    # ------------------------------------------------------------------ state helpers
    def mux(self, port: str, i: int) -> int:
        reg = f"GP{port}MUX1" if i < 16 else f"GP{port}MUX2"
        return (self.get(reg) >> (2 * (i & 15))) & 3

    def direction_bits(self, port: str) -> int:
        return self.get(f"GP{port}DIR")

    def pin_function(self, port: str, i: int) -> str:
        pin = self.pins[port][i]
        m = self.mux(port, i)
        if pin is None:
            return "GPIO" if m == 0 else f"MUX{m}"
        if m == 0:
            return "GPIO"
        fn = pin.functions.get(f"MUX{m}") or ""
        if not fn or fn == "-":
            return "reserved"
        if fn == pin.functions.get("GPIO") or fn == pin.id:
            return "GPIO"
        return fn

    def is_gpio(self, port: str, i: int) -> bool:
        return self.pin_function(port, i) == "GPIO"

    def gpio_by_num(self, num: int) -> tuple[str, int]:
        return PORTS[num // 32], num % 32

    def pin_for(self, num: int) -> Any:
        p, i = self.gpio_by_num(num)
        return self.pins[p][i] if p in self.pins else None

    def xint_pin(self, n: int) -> Any:
        """Source pin of XINTn (1..7) or XNMI (n = 0)."""
        if n == 0:
            sel = self.get("GPIOXNMISEL") & 0x1F
        elif n in (1, 2):
            sel = self.get(f"GPIOXINT{n}SEL") & 0x1F
        else:
            sel = (self.get(f"GPIOXINT{n}SEL") & 0x1F) + 32
        return self.pin_for(sel)

    def pin_config(self, port: str, i: int) -> tuple[str, str, str | None, str | None]:
        """(owner, direction, drive, pull) of pin `port`/`i` from the current registers.  A peripheral output whose
        block says it does not drive the signal now (`signal_driven`, e.g. an ePWM tripped to high impedance) is
        reported as an input with no drive (level from the pull-up, a net or X)."""
        fn = self.pin_function(port, i)
        pull = None if self.get(f"GP{port}PUD") >> i & 1 else "up"
        if fn == "GPIO":
            if self.get(f"GP{port}DIR") >> i & 1:
                return "GPIO", "out", ("H" if self.latch[port] >> i & 1 else "L"), pull
            return "GPIO", "in", None, pull
        d, drv = SIGNALS.pin_state(fn if fn != "reserved" else None)
        live = self._live_state(fn) if fn != "reserved" else None
        if live is not None:
            d, drv = live
        elif d == "out" and self._signal_released(fn):
            d, drv = "in", None
        return fn, d, drv, pull

    def _live_state(self, fn: str) -> tuple[str, str | None] | None:
        """Live (direction, drive) of pin function `fn` from the block that owns one of its '/'-parts: blocks with
        bidirectional or mode-dependent pins (McBSP clocks / frame syncs, SPI master / slave) offer
        `owned_signals()` and `signal_state(sig) -> (direction, idle drive) | None`; None keeps the static role."""
        owners = self._sig_owners
        if owners is None:
            owners = {}
            for b in (getattr(self.chip, "peripherals", None) or {}).values():
                f = getattr(b, "owned_signals", None)
                if f is not None and getattr(b, "signal_state", None) is not None:
                    for s in f():
                        owners[s] = b
            self._sig_owners = owners
        if not owners:
            return None
        for part in fn.split("/"):
            b = owners.get(part.strip())
            if b is not None:
                st = b.signal_state(part.strip())
                if st is not None:
                    return st
        return None

    def _signal_released(self, fn: str) -> bool:
        """Pin function `fn` (or one of its '/'-parts): does the owning block say it does not drive it now?"""
        periphs = getattr(self.chip, "peripherals", None) or {}
        for part in fn.split("/"):
            m = _EPWM_SIG.match(part.strip())
            if m is None:
                continue
            f = getattr(periphs.get(f"EPWM{m[1]}"), "signal_driven", None)
            if f is not None:
                return not f(m[2])
        return False

    def resync_signal(self, sig: str) -> None:
        """A peripheral started or stopped driving `sig`: re-evaluate the pins muxed to it."""
        board = getattr(self.chip, "pins", None)
        if board is None:
            return
        for p in PORTS:
            for i, pin in enumerate(self.pins[p]):
                if pin is not None and self.mux(p, i) and board.signal_matches(pin.owner, sig):
                    self._sync_pin(p, i, quiet=True)
                    self._dat[p] = None

    def _sync_pin(self, port: str, i: int, quiet: bool = False) -> None:
        pin = self.pins[port][i]
        if pin is None or self.chip is None or self.chip.pins is None:
            return
        owner, d, drv, pull = self.pin_config(port, i)
        kw = dict(owner=owner, direction=d, drive=drv)
        old = (pin.owner, pin.direction, pin.pull)
        self.chip.pins.mcu_update(pin, pull=pull, **kw)
        if not quiet and old != (pin.owner, pin.direction, pin.pull):
            d = kw["direction"]
            extra = f" {d}" if d in ("in", "out") else ""
            self.emit("info", block=self.name, pin=pin.id, msg=f"{pin.id} → {kw['owner']}{extra}"
                      + (" pull-up" if pull else ""))

    # ------------------------------------------------------------------ configuration writes
    def _cfg_write(self, blk, reg, old, new):
        port = reg.name[2]
        ch = old ^ new
        if not ch:
            return None
        if "MUX" in reg.name:
            lo = 0 if reg.name.endswith("1") else 16
            pins = [lo + k for k in range(16) if ch >> (2 * k) & 3]
        else:
            pins = [k for k in range(32) if ch >> k & 1]
        self.set(reg.name, new)
        rep = self._rep
        pc = rep.pc() if rep is not None else None
        notify = []
        for i in pins:
            if i >= _PORT_NPINS[port]:
                continue
            if "MUX" in reg.name:
                notify.append(self._owner_block(self._fn_of(port, i, old, reg.name)))
            self._sync_pin(port, i)
            pin = self.pins[port][i]
            if rep is not None and pin is not None:
                rep.config(pin, self._describe(port, i, reg.name), pc)
            if "MUX" in reg.name:
                notify.append(self._owner_block(self.pin_function(port, i)))
        self._dat[port] = None
        for b in dict.fromkeys(x for x in notify if x is not None):
            f = getattr(b, "mux_changed", None)
            if f is not None:
                f()
        return None

    def _fn_of(self, port: str, i: int, old_mux: int, regname: str) -> str:
        """Pin function of `port`/`i` under the previous value `old_mux` of mux register `regname`."""
        m = (old_mux >> (2 * (i & 15))) & 3
        pin = self.pins[port][i]
        if not m or pin is None:
            return "GPIO"
        return pin.functions.get(f"MUX{m}") or ""

    def _owner_block(self, fn: str) -> Any:
        """Block owning a live-role signal of pin function `fn` (see `_live_state`), if any."""
        if not fn or fn == "GPIO":
            return None
        self._live_state("")                               # build the owner map
        for part in fn.split("/"):
            b = (self._sig_owners or {}).get(part.strip())
            if b is not None:
                return b
        return None

    def _describe(self, port: str, i: int, regname: str) -> str:
        if "MUX" in regname:
            return f"mux {self.mux(port, i)} → {self.pin_function(port, i)}"
        if "DIR" in regname:
            return "dir out" if self.get(f"GP{port}DIR") >> i & 1 else "dir in"
        return "pull-up " + ("off" if self.get(f"GP{port}PUD") >> i & 1 else "on")

    def _sel_write(self, blk, reg, old, new):
        self.set(reg.name, new & 0x1F)
        if self._xint is not None:
            self._xint.sources_changed()
        return new & 0x1F

    # ------------------------------------------------------------------ data
    def _set_latch(self, port: str, new: int) -> None:
        new &= (1 << _PORT_NPINS[port]) - 1
        old = self.latch[port]
        self.latch[port] = new
        r = self.regs[f"GP{port}DAT"]
        self._store(r, new)
        ch = old ^ new
        if not ch:
            return
        out = self.get(f"GP{port}DIR") & ch
        if not out:
            return
        pins = self.pins[port]
        mcu = self.chip.pins.mcu_update
        rep = self._rep
        pc = rep.pc() if rep is not None else None
        changes = self.changes[port]
        while out:
            low = out & -out
            i = low.bit_length() - 1
            out ^= low
            pin = pins[i]
            if pin is None or self.mux(port, i) != 0 and not self.is_gpio(port, i):
                continue
            changes[i] += 1
            if rep is not None:
                rep.touch_data(pin, pc)
            mcu(pin, drive="H" if new & low else "L")
        self._dat[port] = None

    def _count_w(self, port: str, mask: int) -> None:
        w = self.writes[port]
        while mask:
            low = mask & -mask
            w[low.bit_length() - 1] += 1
            mask ^= low

    def _dat_write(self, blk, reg, old, new):
        # storage holds the output latch (reads go through _dat_read), so a 16-bit write to one half of GPxDAT
        # merges with the latch of the other half
        port = reg.name[2]
        self._count_w(port, old ^ new)
        self._set_latch(port, new)
        return None

    def _set_write(self, blk, reg, old, new):
        port = reg.name[2]
        self._count_w(port, new)
        self._set_latch(port, self.latch[port] | new)
        return 0

    def _clear_write(self, blk, reg, old, new):
        port = reg.name[2]
        self._count_w(port, new)
        self._set_latch(port, self.latch[port] & ~new)
        return 0

    def _toggle_write(self, blk, reg, old, new):
        port = reg.name[2]
        self._count_w(port, new)
        self._set_latch(port, self.latch[port] ^ new)
        return 0

    def _dat_read(self, blk, reg):
        return self.dat_value(reg.name[2])

    def dat_value(self, port: str) -> int:
        self.n_reads[port] += 1
        v = self._dat[port]
        if v is not None:
            return v
        v = 0
        new_x = []
        ql = self._qlvl
        for i, pin in enumerate(self.pins[port]):
            if pin is None:
                continue
            lvl = (ql.get(pin.id) or pin.level()) if ql else pin.level()
            if lvl == "H":
                v |= 1 << i
            elif lvl == "X" and self.is_gpio(port, i) and not self.get(f"GP{port}DIR") >> i & 1:
                self.x_read[port] |= 1 << i
                if pin.id not in self._warned_x:
                    self._warned_x.add(pin.id)
                    new_x.append(pin.id)
        if new_x:                      # one warning per read that finds newly floating inputs (whole-port reads)
            self.emit("warn", block=self.name, pins=new_x,
                      msg=f"GP{port}DAT read with floating input(s) {', '.join(new_x)} (pull-up disabled, nothing "
                          "connected): read as 0")
        self._dat[port] = v
        return v

    # ------------------------------------------------------------------ fast path for polling GPxDAT
    def read(self, offset: int, size: int) -> int:
        p = _DATA_OFF.get(offset & ~1)
        if p is not None and not self.log_all and (offset & 1) + size <= 2:
            v = self.dat_value(p)
            if size == 2:
                return v
            return (v >> 16) & 0xFFFF if offset & 1 else v & 0xFFFF
        return C2000Block.read(self, offset, size)

    # ------------------------------------------------------------------ pin report
    def pin_stats(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for p in PORTS:
            gin = 0
            for i, pin in enumerate(self.pins[p]):
                if pin is None:
                    continue
                if self.is_gpio(p, i) and not self.get(f"GP{p}DIR") >> i & 1:
                    gin |= 1 << i
            for i, pin in enumerate(self.pins[p]):
                if pin is None:
                    continue
                reads = self.n_reads[p] if gin >> i & 1 else 0
                w, c, fl = self.writes[p][i], self.changes[p][i], bool(self.x_read[p] >> i & 1)
                if reads or w or c or fl:
                    out[pin.id] = {"reads": reads, "writes": w, "changes": c, "floating": fl}
        return out
