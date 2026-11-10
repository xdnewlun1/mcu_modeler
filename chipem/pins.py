"""Generic pin model: per-pin owner/direction/drive/pull plus an external stimulus, resolved to a level.

No family knowledge lives here. Family mux/GPIO models (e.g. Kinetis PORT/GPIO) call `PinBoard.mcu_update`
to tell the board what the MCU does with a pin; the user (CLI/REPL/web) calls `PinBoard.set_external`.
Every observable change is published as a `PinEvent` to subscribers. See docs/CONTRACTS.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, Callable, Iterator, Literal

from .chipdesc import DEFAULT_GPIO_NAME_FORMAT, format_gpio_name

Level = Literal["H", "L", "X"]
LEVELS = ("H", "L", "X")

_KEEP: Any = object()   # sentinel: "leave this attribute unchanged" in mcu_update


@dataclass(frozen=True)
class PinEvent:
    t_ns: float
    pin: str            # short pin name (e.g. "PTC5", "PA5"), see Pin.id
    field: str          # "level" | "owner" | "direction" | "pull"
    old: Any
    new: Any


def _norm_level(v: Any) -> Level | None:
    """Accept H/L/X, 1/0, True/False, "1"/"0", "high"/"low"; None stays None."""
    if v is None:
        return None
    if isinstance(v, bool) or isinstance(v, int):
        return "H" if v else "L"
    s = str(v).strip().upper()
    if s in ("H", "HIGH", "1", "ON", "TRUE"):
        return "H"
    if s in ("L", "LOW", "0", "OFF", "FALSE"):
        return "L"
    if s in ("X", "Z", "FLOAT", "FLOATING"):
        return "X"
    raise ValueError(f"bad pin level {v!r}")


@dataclass(eq=False)
class Pin:
    num: int
    name: str                                   # datasheet name, e.g. "PTC5/LLWU_P9"
    functions: dict[str, str] = field(default_factory=dict)
    kind: str = "gpio"                          # gpio | analog | power | special
    port: str | None = None
    bit: int | None = None
    default: str | None = None
    owner: str = "disabled"                     # "GPIO" | function name | "analog" | "disabled"
    direction: str = "none"                     # "in" | "out" | "analog" | "none"
    drive: Level | None = None                  # level driven when direction == "out"
    pull: str | None = None                     # "up" | "down" | None
    external: Level | None = None               # user stimulus; None = nothing connected
    fixed: Level | None = None                  # power pins: constant level (VDD -> H, VSS -> L)
    gpio_name_format: str = DEFAULT_GPIO_NAME_FORMAT   # from the chip description (chipdesc.PinDesc.gpio_name_format)
    od_low: bool = False                        # an open-drain peripheral signal (I2C SDA/SCL) pulls the pin low

    @property
    def gpio_name(self) -> str | None:
        """Short GPIO name from the chip's `gpio_name_format`: Kinetis "PTC5", STM32 "PC5", C2000 "GPIO34"."""
        return format_gpio_name(self.gpio_name_format, self.port, self.bit)

    @cached_property
    def id(self) -> str:
        """Short identifier used in events and listener maps: the GPIO name ("PTC5", "PA5") for GPIO pins, else
        the first name part. Cached: name, port and bit never change after construction (hot path: every pin
        event)."""
        return self.gpio_name or self.name.split("/")[0]

    @property
    def aliases(self) -> list[str]:
        out = [self.name, self.id, *self.name.split("/")]
        return list(dict.fromkeys(out))

    def level(self) -> Level:
        if self.fixed is not None:
            return self.fixed
        if self.od_low:
            return "L"
        if self.direction == "out":
            return self.drive if self.drive is not None else "X"
        if self.external is not None:
            return self.external
        if self.pull == "up":
            return "H"
        if self.pull == "down":
            return "L"
        return "X"

    def describe(self) -> str:
        bits = [f"{self.num:3d} {self.id:<10} {self.level()}", f"owner={self.owner}", f"dir={self.direction}"]
        if self.direction == "out":
            bits.append(f"drive={self.drive}")
        if self.od_low:
            bits.append("od=L")
        if self.pull:
            bits.append(f"pull={self.pull}")
        if self.external is not None:
            bits.append(f"ext={self.external}")
        return " ".join(bits)


class _ListenerMap:
    """pin name (any alias or number) -> list of callbacks cb(pin). Keys are normalised to Pin.id."""

    def __init__(self, board: PinBoard):
        self._board = board
        self._d: dict[str, list[Callable[[Pin], None]]] = {}

    def _key(self, k: Any) -> str:
        return self._board.get(k).id

    def __getitem__(self, k: Any) -> list[Callable[[Pin], None]]:
        return self._d.setdefault(self._key(k), [])

    def setdefault(self, k: Any, default: list | None = None) -> list:
        key = self._key(k)
        if key not in self._d:
            self._d[key] = default if default is not None else []
        return self._d[key]

    def get(self, k: Any, default=None):
        try:
            return self._d.get(self._key(k), default)
        except KeyError:
            return default

    def __contains__(self, k: Any) -> bool:
        try:
            return self._key(k) in self._d
        except KeyError:
            return False

    def items(self):
        return self._d.items()

    def clear(self):
        self._d.clear()


def _fixed_level(name: str, kind: str) -> Level | None:
    if kind != "power":
        return None
    n = name.upper()
    return "L" if ("VSS" in n or "VREFL" in n) else "H"


class PinBoard:
    def __init__(self, pins: list[Pin], clock: Any = None):
        self.pins: list[Pin] = list(pins)
        self.clock = clock
        self.time_fn: Callable[[], float] | None = None    # precise virtual time (set by the Chip)
        self._subs: list[Callable[[PinEvent], None]] = []
        self.input_listeners = _ListenerMap(self)
        self._by_num: dict[int, Pin] = {}
        self._by_name: dict[str, Pin] = {}
        self._by_port: dict[tuple[str, int], Pin] = {}
        for p in self.pins:
            self._by_num[p.num] = p
            if p.port is not None and p.bit is not None:
                self._by_port.setdefault((str(p.port).upper(), int(p.bit)), p)
            for a in p.aliases:
                self._by_name.setdefault(a.upper(), p)
        self._initial = {p.num: (p.owner, p.direction, p.drive, p.pull) for p in self.pins}
        # live levels of peripheral output signals (timer/PWM channels): a pin owned by such a signal drives this
        # level instead of the signal's idle level (drive_signal / release_signal)
        self.signal_levels: dict[str, Level] = {}
        self._signal_pins: dict[str, list[Pin]] = {}
        self._mux_drive: dict[int, Level | None] = {}       # pin num -> drive the mux model last asked for
        # open-drain peripheral outputs (I2C SDA/SCL) currently pulling their pins low (drive_open_drain)
        self.od_low: set[str] = set()

    # --- construction --------------------------------------------------
    @classmethod
    def from_desc(cls, desc: Any, clock: Any = None) -> PinBoard:
        pins = []
        for d in desc.pins:
            default = d.default
            if d.kind == "gpio":
                owner = "disabled" if default in (None, "", "DISABLED") else default
                direction = "none"
            elif d.kind == "analog":
                owner, direction = "analog", "analog"
            else:
                owner, direction = d.kind, "none"
            pins.append(Pin(num=d.num, name=d.name, functions=dict(d.functions), kind=d.kind, port=d.port,
                            bit=d.bit, default=default, owner=owner, direction=direction,
                            fixed=_fixed_level(d.name, d.kind),
                            gpio_name_format=getattr(d, "gpio_name_format", None)
                            or getattr(desc, "gpio_name_format", None) or DEFAULT_GPIO_NAME_FORMAT))
        return cls(pins, clock)

    # --- lookup --------------------------------------------------------
    def get(self, name_or_num: int | str | Pin) -> Pin:
        if isinstance(name_or_num, Pin):
            return name_or_num
        if isinstance(name_or_num, int):
            return self._by_num[name_or_num]
        s = str(name_or_num).strip()
        if s.isdigit():
            return self._by_num[int(s)]
        try:
            return self._by_name[s.upper()]
        except KeyError:
            raise KeyError(f"unknown pin {name_or_num!r}") from None

    def __iter__(self) -> Iterator[Pin]:
        return iter(self.pins)

    def __len__(self) -> int:
        return len(self.pins)

    def gpio(self, port: str, bit: int) -> Pin | None:
        """The pin of GPIO port `port` ("A".."I"), bit `bit`, whatever the chip's naming scheme."""
        return self._by_port.get((str(port).upper(), int(bit)))

    def levels(self) -> dict[str, Level]:
        return {p.id: p.level() for p in self.pins}

    # --- events --------------------------------------------------------
    def subscribe(self, cb: Callable[[PinEvent], None]) -> Callable[[], None]:
        self._subs.append(cb)
        return lambda: self._subs.remove(cb) if cb in self._subs else None

    def _now(self) -> float:
        if self.time_fn is not None:
            return float(self.time_fn())
        c = self.clock
        return float(c.now_ns()) if c is not None and hasattr(c, "now_ns") else 0.0

    def _emit(self, pin: Pin, fld: str, old: Any, new: Any, t: float):
        if not self._subs:
            return
        ev = PinEvent(t, pin.id, fld, old, new)
        for cb in list(self._subs):
            cb(ev)

    def _level_changed(self, pin: Pin, old: Level, t: float):
        new = pin.level()
        if new != old:
            self._emit(pin, "level", old, new, t)
            for cb in list(self.input_listeners.get(pin.id) or ()):
                cb(pin)

    # --- stimulus / MCU side -------------------------------------------
    def set_external(self, name: Any, level: Any) -> Pin:
        pin = self.get(name)
        lvl = _norm_level(level)
        old = pin.level()
        pin.external = lvl
        self._level_changed(pin, old, self._now())
        return pin

    def mcu_update(self, name: Any, *, owner: Any = _KEEP, direction: Any = _KEEP, drive: Any = _KEEP,
                   pull: Any = _KEEP) -> Pin:
        """Update MCU-side state. Omitted arguments are unchanged; `owner=None`/`direction=None` also mean
        unchanged, while `drive=None` / `pull=None` explicitly clear the drive level / pull."""
        pin = self.get(name)
        t = self._now()
        old_level = pin.level()
        if owner is not _KEEP and owner is not None and owner != pin.owner:
            o, pin.owner = pin.owner, owner
            self._emit(pin, "owner", o, owner, t)
        if direction is not _KEEP and direction is not None and direction != pin.direction:
            if direction not in ("in", "out", "analog", "none"):
                raise ValueError(f"bad direction {direction!r}")
            o, pin.direction = pin.direction, direction
            self._emit(pin, "direction", o, direction, t)
        if drive is not _KEEP:
            self._mux_drive[pin.num] = _norm_level(drive)
            if self.signal_levels and pin.direction == "out" and pin.owner not in ("GPIO", "disabled"):
                lvl = self._signal_level(pin.owner)
                if lvl is not None:
                    drive = lvl                  # a driven peripheral signal: its live level, not the idle one
            pin.drive = _norm_level(drive)
        if pull is not _KEEP and pull != pin.pull:
            if pull not in ("up", "down", None):
                raise ValueError(f"bad pull {pull!r}")
            o, pin.pull = pin.pull, pull
            self._emit(pin, "pull", o, pull, t)
        self._level_changed(pin, old_level, t)
        if self.od_low or pin.od_low:
            self._od_sync(pin, t)
        return pin

    # --- open-drain peripheral outputs (generic: names only) -----------------------------------------------------
    def _od_want(self, pin: Pin) -> bool:
        return pin.direction != "analog" and any(self.signal_matches(pin.owner, s) for s in self.od_low)

    def _od_sync(self, pin: Pin, t: float) -> None:
        want = self._od_want(pin)
        if want != pin.od_low:
            old = pin.level()
            pin.od_low = want
            self._level_changed(pin, old, t)
            self._emit(pin, "od", not want, want, t)     # after the level: a board net re-resolves on this event

    def drive_open_drain(self, name: str, low: bool) -> None:
        """Open-drain peripheral output `name` (I2C "I2C0_SDA", "SCLA", ...): `low=True` pulls every pin currently
        owned by it low (`Pin.od_low`: the pin reads L and is a strong L driver on a board net, whatever direction
        its mux model reported); `low=False` releases it, and the pin shows what its mux model says again (an
        open-drain role is an input: the level comes from the net, a pull or stimulus, else X). A pin muxed to the
        signal later follows at once (mcu_update). Emits a PinEvent field "od" (old, new: bool) and the level
        change, at instruction-precise time."""
        low = bool(low)
        if (name in self.od_low) == low:
            return
        if low:
            self.od_low.add(name)
        else:
            self.od_low.discard(name)
        t = None
        for p in self.signal_pins(name):
            if not self.signal_matches(p.owner, name):
                continue
            want = self._od_want(p)
            if want != p.od_low:
                if t is None:
                    t = self._now()
                old = p.level()
                p.od_low = want
                self._level_changed(p, old, t)
                self._emit(p, "od", not want, want, t)  # after the level: a board net re-resolves on this event

    def signal_level(self, name: str) -> Level | None:
        """Wired-AND level of the pins currently owned by peripheral signal `name` (any L -> L, else H if one is H,
        else X), or None when no pin carries the signal (not muxed)."""
        out = None
        for p in self.signal_pins(name):
            if self.signal_matches(p.owner, name):
                lvl = p.level()
                if lvl == "L":
                    return "L"
                if out is None or lvl == "H":
                    out = lvl
        return out

    # --- peripheral output signals (generic: names only, no vendor knowledge) --------------------------------------
    @staticmethod
    def _signal_rank(owner: str | None, name: str) -> int:
        """0 = `owner` does not carry `name`; 2 = exact (a whole '/'-part of the owner is `name`); 1 = the owner
        bundles `name` with a letters-only qualifier ("TIM2_CH1_ETR" carries "TIM2_CH1"). A suffix with digits is a
        different signal: "TIM1" does not match "TIM1_CH1", "FTM0_CH1" does not match "FTM0_CH10"."""
        if not owner or not name:
            return 0
        best = 0
        for part in str(owner).split("/"):
            part = part.strip()
            if part == name:
                return 2
            if part.startswith(name + "_"):
                rest = part[len(name) + 1:]
                if rest and all(q.isalpha() for q in rest.split("_")):
                    best = 1
        return best

    @classmethod
    def signal_matches(cls, owner: str | None, name: str) -> bool:
        """Does pin owner `owner` carry signal `name`? Exact, a '/'-part of a shared function ("A/B"), or a function
        that bundles more signals on one pin with a letters-only qualifier ("TIM2_CH1_ETR" carries "TIM2_CH1")."""
        return cls._signal_rank(owner, name) > 0

    def _signal_level(self, owner: str) -> Level | None:
        """Live level for a pin owned by `owner`: the driven signal that is exactly the owner (or one of its
        '/'-parts) wins over a bundled match; among equal matches the longest signal name wins, then the name
        (deterministic whatever the order in which the signals were first driven)."""
        best, key = None, None
        for name, lvl in self.signal_levels.items():
            r = self._signal_rank(owner, name)
            if r:
                k = (r, len(name), name)
                if key is None or k > key:
                    best, key = lvl, k
        return best

    def signal_pins(self, name: str) -> list[Pin]:
        """Pins that offer signal `name` in any of their functions (static, from the chip description)."""
        pins = self._signal_pins.get(name)
        if pins is None:
            pins = [p for p in self.pins if any(self.signal_matches(f, name) for f in p.functions.values())]
            self._signal_pins[name] = pins
        return pins

    def drive_signal(self, name: str, level: Level) -> None:
        """A peripheral output signal (e.g. "TIM2_CH1", "FTM0_CH3", "EPWM1A") is now at `level`: every pin currently
        owned by it as an output follows (instruction-precise time, like any MCU drive change)."""
        lvl = _norm_level(level)
        if self.signal_levels.get(name) == lvl:
            return
        self.signal_levels[name] = lvl
        t = None
        for p in self.signal_pins(name):
            if p.direction != "out" or not self.signal_matches(p.owner, name):
                continue
            plvl = self._signal_level(p.owner)     # the owner's best signal (an exact match wins over a bundle)
            if plvl is not None and p.drive != plvl:
                if t is None:
                    t = self._now()
                old = p.level()
                p.drive = plvl                       # not via mcu_update: keep the mux model's requested drive
                self._level_changed(p, old, t)

    def release_signal(self, name: str, idle: Level | None = None) -> None:
        """The peripheral no longer drives `name` (output disabled, module off). Its output pins show `idle` if
        given, else the drive the mux model last reported for them (the signal's idle level from the family table),
        or the live level of another still-driven signal the owner carries."""
        if self.signal_levels.pop(name, None) is None:
            return
        idle = _norm_level(idle)
        for p in self.signal_pins(name):
            if p.direction != "out" or not self.signal_matches(p.owner, name):
                continue
            lvl = self._signal_level(p.owner) if self.signal_levels else None
            if lvl is None:
                lvl = idle if idle is not None else self._mux_drive.get(p.num, p.drive)
            if p.drive != lvl:
                old = p.level()
                p.drive = lvl                    # not via mcu_update: keep the mux model's requested drive
                self._level_changed(p, old, self._now())

    def reset(self):
        """MCU reset: restore power-on owner/direction/drive/pull (external stimulus is kept). Emits events."""
        self.signal_levels.clear()
        self.od_low.clear()
        for p in self.pins:
            owner, direction, drive, pull = self._initial[p.num]
            self.mcu_update(p, owner=owner, direction=direction, drive=drive, pull=pull)
