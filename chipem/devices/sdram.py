"""Board-level SDR SDRAM (generic, vendor-free; JEDEC-style command interface): `banks` x `rows` x `cols` locations
of `width` bits (8, 16 or 32), e.g. the ISSI IS42S16400J (4 x 4096 x 256 x 16 bit = 8 MB).

The device knows nothing about the controller in front of it.  A chip's SDRAM controller (any peripheral with an
`attach_sdram(dev, bank)` method, e.g. the STM32F42x/43x `stm32.fmc`) drives its command interface and moves data
through `read(loc)` / `write(loc, value, mask)` or the `mem` buffer (location = (bank * rows + row) * cols + col,
i.e. linear in the bank|row|column order that the STM32 FMC uses on its AHB address lines, RM0090 Tables 259-262).

Power-up and initialization (JEDEC SDR SDRAM, e.g. IS42S16400J data sheet "Power-up and initialization"):
  1. CKE high with a running clock (`clock_enable(t_ns)`), then at least `t_pu_us` (default 100 us) of NOP;
  2. PRECHARGE ALL (`precharge_all(t_ns)`): issued earlier than `t_pu_us` after CKE it is ignored (one `warn`);
  3. at least `init_refreshes` (default 2) AUTO REFRESH commands (`auto_refresh(n)`);
  4. LOAD MODE REGISTER (`load_mode(mrd)`).
Until the sequence is complete the device does not answer: `ready` is False and the controller must treat its data
bus as floating.  A controller reset / chip reset that drops CKE (`clock_disable()`) requires the whole sequence
again (data is kept as long as it is refreshed, see below).
Mode register (`mode`): burst length M[2:0], burst type M3, CAS latency M[6:4], operating mode M[8:7], write burst
mode M9; `cas_latency` and `burst_length` are decoded for the controller's consistency checks.

Data retention: each cell must be refreshed every `refresh_ms` (default 64 ms; one AUTO REFRESH refreshes one row in
every bank, so `rows` refreshes per period).  The controller reports how often it refreshes with
`set_refresh(interval_ns)` (None = no refresh) and the device's own self-refresh with `self_refresh(on)`.  When the
refresh is missing or too slow (rows x interval > refresh_ms) the device schedules the loss of its content
`refresh_ms` after that moment (`schedule(t_abs_ns, cb)` / `cancel(handle)` supplied by the controller, i.e. the
chip scheduler) and then fills the array with `decay` (default 0x00; real cells drift to their ground state); one
`lost` callback per loss.  No per-access work is needed for this.  Power-up content: `fill` (default 0).
"""
from __future__ import annotations

from array import array
from pathlib import Path
from typing import Any, Callable

from .spi_flash import _parse_size

_TYPECODE = {8: "B", 16: "H", 32: "L"}


class Sdram:
    def __init__(self, banks: int = 4, rows: int = 4096, cols: int = 256, width: int = 16, *,
                 refresh_ms: float = 64.0, t_pu_us: float = 100.0, init_refreshes: int = 2, fill: int = 0,
                 decay: int = 0, image: str | Path | None = None, name: str = "sdram"):
        self.name = name
        self.banks, self.rows, self.cols, self.width = int(banks), int(rows), int(cols), int(width)
        if self.width not in _TYPECODE:
            raise ValueError(f"sdram: width must be 8, 16 or 32, not {width}")
        if self.banks not in (2, 4) or self.rows < 1 or self.cols < 1:
            raise ValueError("sdram: banks must be 2 or 4, rows/cols > 0")
        self.size = self.banks * self.rows * self.cols            # locations
        self.mask = (1 << self.width) - 1
        self.mem = array(_TYPECODE[self.width], [int(fill) & self.mask]) * self.size
        if image is not None:
            self.load(Path(image).read_bytes())
        self.refresh_ms = float(refresh_ms)
        self.t_pu_ns = float(t_pu_us) * 1e3
        self.init_refreshes = int(init_refreshes)
        self.decay = int(decay) & self.mask
        self.reads = 0
        self.writes = 0
        self.losses = 0
        self.attached: list[tuple[Any, str]] = []
        # command-interface state
        self.cke = False
        self.cke_t = 0.0
        self.state = "off"           # off | powerup | precharged | refreshed | ready
        self.refreshes = 0            # AUTO REFRESH count since the PRECHARGE ALL of the init sequence
        self.mode: int | None = None  # last loaded mode register
        self.in_self_refresh = False
        self.notes: list[str] = []    # warnings for the controller to emit
        # retention
        self.schedule: Callable[[float, Callable[[], None]], Any] | None = None
        self.cancel: Callable[[Any], None] | None = None
        self.now: Callable[[], float] = lambda: 0.0
        self.lost: Callable[[str], None] | None = None
        self._refresh_ns: float | None = None
        self._decay_ev: Any = None

    @property
    def nbytes(self) -> int:
        return self.size * self.width // 8

    @property
    def ready(self) -> bool:
        return self.cke and self.state == "ready"

    @property
    def cas_latency(self) -> int | None:
        return None if self.mode is None else (self.mode >> 4) & 7

    @property
    def burst_length(self) -> int | None:
        if self.mode is None:
            return None
        bl = self.mode & 7
        return {0: 1, 1: 2, 2: 4, 3: 8, 7: 0}.get(bl, -1)          # 0 = full page, -1 = reserved

    def geometry(self) -> tuple[int, int, int, int]:
        return self.banks, self.rows, self.cols, self.width

    def load(self, data: bytes, loc: int = 0) -> None:
        nb = self.width // 8
        for i in range(min(len(data) // nb, self.size - loc)):
            self.mem[loc + i] = int.from_bytes(data[i * nb:(i + 1) * nb], "little")

    # ------------------------------------------------------------------ data
    def read(self, loc: int) -> int:
        self.reads += 1
        return self.mem[loc % self.size]

    def write(self, loc: int, value: int, mask: int | None = None) -> None:
        self.writes += 1
        i = loc % self.size
        if mask is None:
            self.mem[i] = value & self.mask
        else:
            mask &= self.mask
            self.mem[i] = (self.mem[i] & ~mask) | (value & mask)

    # ------------------------------------------------------------------ commands
    def _note(self, msg: str) -> None:
        self.notes.append(msg)

    def clock_enable(self, t_ns: float) -> None:
        """CKE high with a stable clock (controller "clock configuration enable"): starts the power-up delay."""
        if not self.cke:
            self.cke = True
            self.cke_t = float(t_ns)
            self.state = "powerup"
            self.refreshes = 0

    def clock_disable(self) -> None:
        """CKE low / clock gone (controller reset, SDCLK disabled): the device must be initialized again."""
        self.cke = False
        self.state = "off"
        self.in_self_refresh = False

    def precharge_all(self, t_ns: float) -> None:
        if not self.cke:
            return
        if self.state == "powerup":
            dt = float(t_ns) - self.cke_t
            if dt < self.t_pu_ns:
                self._note(f"{self.name}: PRECHARGE ALL {dt / 1e3:.1f} us after CKE (power-up delay "
                           f"{self.t_pu_ns / 1e3:g} us not met): ignored, the device is not initialized")
                return
            self.state = "precharged"
            self.refreshes = 0

    def auto_refresh(self, n: int = 1) -> None:
        if not self.cke:
            return
        if self.state in ("precharged", "refreshed"):
            self.refreshes += n
            if self.refreshes >= self.init_refreshes:
                self.state = "refreshed"
        elif self.state == "powerup":
            self._note(f"{self.name}: AUTO REFRESH before PRECHARGE ALL during initialization: ignored")

    def load_mode(self, mrd: int) -> None:
        if not self.cke:
            return
        if self.state == "refreshed" or self.state == "ready":
            self.mode = int(mrd) & 0x1FFF
            self.state = "ready"
        else:
            self._note(f"{self.name}: LOAD MODE REGISTER before PRECHARGE ALL + {self.init_refreshes} AUTO REFRESH "
                       f"(state {self.state}): ignored, the device is not initialized")

    def self_refresh(self, on: bool) -> None:
        self.in_self_refresh = bool(on) and self.cke
        self._retention_update()

    # ------------------------------------------------------------------ retention
    def set_refresh(self, interval_ns: float | None) -> None:
        """The controller now issues one AUTO REFRESH every `interval_ns` (None: no refresh at all)."""
        self._refresh_ns = interval_ns if interval_ns and interval_ns > 0 else None
        self._retention_update()

    def refreshed_ok(self) -> bool:
        if self.in_self_refresh:
            return True
        if self._refresh_ns is None:
            return False
        return self._refresh_ns * self.rows <= self.refresh_ms * 1e6 * (1 + 1e-9)

    def _retention_update(self) -> None:
        ok = self.refreshed_ok()
        if ok:
            if self._decay_ev is not None and self.cancel is not None:
                self.cancel(self._decay_ev)
            self._decay_ev = None
            return
        if self._decay_ev is None and self.schedule is not None:
            self._decay_ev = self.schedule(self.now() + self.refresh_ms * 1e6, self._decay)

    def _decay(self) -> None:
        self._decay_ev = None
        if self.refreshed_ok():
            return
        nb = self.width // 8
        memoryview(self.mem).cast("B")[:] = self.decay.to_bytes(nb, "little") * self.size   # in place (may be mapped)
        self.losses += 1
        why = ("no refresh" if self._refresh_ns is None else
               f"refresh every {self._refresh_ns / 1e3:.2f} us x {self.rows} rows > {self.refresh_ms:g} ms")
        if self.lost is not None:
            self.lost(f"{self.name}: data lost ({why} for {self.refresh_ms:g} ms; retention exceeded)")

    # ------------------------------------------------------------------ attach to a chip
    def attach(self, chip: Any, bus: str, bank: int | None = None) -> "Sdram":
        blk = chip.peripherals.get(bus) if chip is not None else None
        if blk is None or not hasattr(blk, "attach_sdram"):
            have = [n for n, b in (getattr(chip, "peripherals", {}) or {}).items() if hasattr(b, "attach_sdram")]
            raise ValueError(f"sdram: {bus!r} is not an SDRAM controller of this chip (have: {', '.join(have) or 'none'})")
        blk.attach_sdram(self, bank)
        self.attached.append((chip, bus))
        return self


def parse_spec(spec: str) -> tuple[str, dict]:
    """"FMC:bank=2,size=8M,width=16" -> ("FMC", {...})."""
    bus, _, rest = spec.partition(":")
    opts: dict[str, Any] = {}
    for part in filter(None, (p.strip() for p in rest.split(","))):
        k, _, v = part.partition("=")
        opts[k.strip().lower()] = v.strip()
    return bus.strip().upper(), opts


def from_options(o: dict, name: str = "sdram") -> Sdram:
    """Device from board/CLI options: banks, rows, cols, width (default IS42S16400J: 4 x 4096 x 256 x 16) or
    `size` (bytes, e.g. 8M / 32M: rows derived with the other defaults), refresh_ms, t_pu_us, init_refreshes,
    fill, decay, file."""
    width = int(o.get("width", 16))
    banks = int(o.get("banks", 4))
    cols = int(o.get("cols", 256))
    if "rows" in o:
        rows = int(o["rows"])
    elif "size" in o:
        rows = _parse_size(o["size"]) // (banks * cols * width // 8)
    else:
        rows = 4096
    return Sdram(banks, rows, cols, width, refresh_ms=float(o.get("refresh_ms", 64)),
                 t_pu_us=float(o.get("t_pu_us", 100)), init_refreshes=int(o.get("init_refreshes", 2)),
                 fill=int(str(o.get("fill", 0)), 0), decay=int(str(o.get("decay", 0)), 0), image=o.get("file"),
                 name=name)


def attach_from_spec(chip: Any, spec: str) -> Sdram:
    bus, o = parse_spec(spec)
    bank = int(o.pop("bank")) if "bank" in o else None
    dev = from_options(o, name=f"{bus}_sdram{bank or ''}")
    return dev.attach(chip, bus, bank)
