"""Pin discovery report: what did the firmware do with each pin, and which code did it?

Answers "I have firmware but no schematic: which pins does it use, and how?" from a run, plus the static
guess from `chipem.firmware.analyze`.

Recording (always on, cheap):
* `PinReport(chip)` is created on demand by `ensure(chip)`; the Kinetis PORT/GPIO models call it in their
  `attach()`, so every Chip with pin muxing has `chip.pin_report` before the first instruction runs.
* Family models call `config(pin, what, pc)` for configuration changes (PCR mux/pull/IRQC, PDDR direction)
  and `touch_cfg` / `touch_data(pin, pc)` when code touches a pin's control or data registers. These are
  config-rate or slow-path calls (first write of a new value, level changes, PDIR cache misses); the PC is
  read from the core inside the MMIO callback, where Unicorn reports the address of the accessing load/store
  instruction (verified in tests/test_pinreport.py, fast and precise mode).
* Hot-path counters (PDIR reads, PSOR/PCOR/PTOR writes, level changes, ISF sets) live in the family models as
  plain ints/lists and are pulled through `blk.pin_stats() -> {pin_id: {...}}` only when a report is built.
* A PinBoard subscription keeps a ring of level-change times per pin (for toggle periods). Without any
  family hooks (`hooked` False) it also records owner/direction/pull changes as history, without PCs.

Rendering: `format_pin_report(chip)` (table + discovery summary + static comparison), `pin_history(chip, name)`
(one pin), `report_dict(chip)` (JSON for the web UI).
"""
from __future__ import annotations

import re
import statistics
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .symbols import Symbols

RING = 1024                     # level-change timestamps kept per pin (for period estimation)
MAX_DATA_PCS = 8
HIST_HEAD = 64                  # configuration history kept per pin: the first HIST_HEAD entries ...
HIST_TAIL = 64                  # ... and the last HIST_TAIL (counts by category are always exact)
_MUX_FN = re.compile(r"mux (?:ALT|AF)\d+ \(([^)]*)\)")     # Kinetis "mux ALT2 (UART1_TX)", STM32 "mux AF7 (USART1_TX)"


@dataclass(eq=False)
class PinRecord:
    pin: Any
    head: list[tuple[float, int | None, str]] = field(default_factory=list)   # (t_ns, pc, what)
    tail: deque = field(default_factory=lambda: deque(maxlen=HIST_TAIL))
    n_hist: int = 0
    cats: dict[str, int] = field(default_factory=dict)       # "mux"/"dir"/"pull"/"irq"/... -> changes
    funcs: dict[str, int] = field(default_factory=dict)      # mux target function -> times selected
    cfg_first_pc: int | None = None
    cfg_last_pc: int | None = None
    data_first_pc: int | None = None
    data_last_pc: int | None = None
    data_pcs: list[int] = field(default_factory=list)
    isr_pc: int | None = None            # last code that cleared the pin's interrupt flag
    level_events: int = 0
    edge_times: deque = field(default_factory=lambda: deque(maxlen=RING))
    edge_levels: deque = field(default_factory=lambda: deque(maxlen=RING))   # new level of each edge in edge_times

    def add(self, t: float, pc: int | None, what: str) -> None:
        e = (t, pc, what)
        if len(self.head) < HIST_HEAD:
            self.head.append(e)
        else:
            self.tail.append(e)
        self.n_hist += 1
        for part in what.split(", "):
            c = part.split(" ", 1)[0]
            self.cats[c] = self.cats.get(c, 0) + 1
            m = _MUX_FN.match(part)
            if m and not part.endswith("(unchanged)"):
                self.funcs[m.group(1)] = self.funcs.get(m.group(1), 0) + 1
            elif m:
                self.funcs.setdefault(m.group(1), 1)

    @property
    def history(self) -> list[tuple[float, int | None, str]]:
        """Kept entries (first HIST_HEAD + last HIST_TAIL); `omitted` tells how many are missing between."""
        return self.head + list(self.tail)

    @property
    def omitted(self) -> int:
        return self.n_hist - len(self.head) - len(self.tail)

    @property
    def last(self) -> tuple[float, int | None, str] | None:
        if self.tail:
            return self.tail[-1]
        return self.head[-1] if self.head else None


def ensure(chip: Any) -> "PinReport | None":
    """The chip's PinReport, created (and subscribed to the PinBoard) on first use."""
    rep = getattr(chip, "pin_report", None)
    if rep is None and getattr(chip, "pins", None) is not None:
        rep = PinReport(chip)
        try:
            chip.pin_report = rep
        except AttributeError:
            pass
    return rep


class PinReport:
    def __init__(self, chip: Any):
        self.chip = chip
        self.hooked = False                     # a family model reports configuration with PCs
        self.recs: dict[str, PinRecord] = {}
        self._syms: Symbols | None = None
        self._syms_img: Any = None
        self._static: Any = None
        self._static_img: Any = None
        board = chip.pins
        self._unsub = board.subscribe(self._on_pin) if board is not None else None

    # ------------------------------------------------------------------ recording API (family models)
    def pc(self) -> int | None:
        core = getattr(self.chip, "core", None)
        if core is None:
            return None
        try:
            if getattr(core, "running", False) and type(core).__name__ == "C28xCore":
                return _c28x_live_pc(core)
            live = getattr(core, "live_pc", None)              # interpreters that keep the PC in a local (MCS-51)
            if live is not None and getattr(core, "running", False):
                return live()
            return core.pc
        except Exception:
            return None

    def now(self) -> float:
        c = self.chip
        f = getattr(c, "now_ns", None)
        try:
            return float(f()) if f is not None else float(c.clock.now_ns())
        except Exception:
            return 0.0

    def rec(self, pin: Any) -> PinRecord:
        r = self.recs.get(pin.id)
        if r is None:
            r = self.recs[pin.id] = PinRecord(pin)
        return r

    def config(self, pin: Any, what: str, pc: int | None = None) -> None:
        """A configuration change of `pin` made by the instruction at `pc` (now)."""
        r = self.rec(pin)
        r.add(self.now(), pc, what)
        self.touch_cfg(pin, pc, r)

    def touch_cfg(self, pin: Any, pc: int | None, r: PinRecord | None = None) -> None:
        r = r or self.rec(pin)
        if pc is None:
            return
        if r.cfg_first_pc is None:
            r.cfg_first_pc = pc
        r.cfg_last_pc = pc

    def touch_data(self, pin: Any, pc: int | None) -> None:
        r = self.rec(pin)
        if pc is None:
            return
        if r.data_first_pc is None:
            r.data_first_pc = pc
        r.data_last_pc = pc
        if pc not in r.data_pcs and len(r.data_pcs) < MAX_DATA_PCS:
            r.data_pcs.append(pc)

    def touch_isr(self, pin: Any, pc: int | None) -> None:
        """Code at pc cleared this pin's interrupt flag (interrupt handler attribution)."""
        if pc is not None:
            self.rec(pin).isr_pc = pc

    # ------------------------------------------------------------------ PinBoard subscription
    def _on_pin(self, ev: Any) -> None:
        if ev.field == "level":
            r = self.recs.get(ev.pin)
            if r is None:
                try:
                    r = self.rec(self.chip.pins.get(ev.pin))
                except KeyError:
                    return
            r.level_events += 1
            r.edge_times.append(ev.t_ns)
            r.edge_levels.append(ev.new)
        elif not self.hooked:
            try:
                pin = self.chip.pins.get(ev.pin)
            except KeyError:
                return
            self.rec(pin).add(ev.t_ns, None, f"{ev.field} {ev.new}")

    # ------------------------------------------------------------------ helpers
    @property
    def symbols(self) -> Symbols:
        img = getattr(self.chip, "firmware", None)
        if self._syms is None or self._syms_img is not img:
            self._syms = Symbols.for_image(img)
            self._syms_img = img
        return self._syms

    def where(self, pc: int | None) -> str:
        return "-" if pc is None else self.symbols.name(pc)

    def stats(self) -> dict[str, dict]:
        """Merged per-pin counters from every family model implementing pin_stats()."""
        out: dict[str, dict] = {}
        for blk in getattr(self.chip, "peripherals", {}).values():
            f = getattr(blk, "pin_stats", None)
            if f is None:
                continue
            try:
                st = f()
            except Exception:
                continue
            for pid, d in st.items():
                tgt = out.setdefault(pid, {})
                for k, v in d.items():
                    if isinstance(v, bool):
                        tgt[k] = tgt.get(k, False) or v
                    elif isinstance(v, (int, float)):
                        tgt[k] = tgt.get(k, 0) + v
                    elif v is not None:
                        tgt[k] = v
        return out

    def static(self) -> Any:
        """chipem.firmware.analyze() of the loaded image (cached), or None."""
        img = getattr(self.chip, "firmware", None)
        if img is None:
            return None
        if self._static_img is not img:
            self._static_img = img
            try:
                from .firmware import analyze
                self._static = analyze(img, self.chip.desc, disasm=True, time_budget=4.0)
            except Exception:
                self._static = None
        return self._static


# ---------------------------------------------------------------------------------------------------------------
# rendering

_PIN_RE = re.compile(r"\b[A-Z][A-Z0-9]*?\d+\b")      # candidate pin ids; filtered against the chip's GPIO names


def _fmt_t(t: float | None) -> str:
    if t is None:
        return "-"
    if t >= 1e9:
        return f"{t / 1e9:.3f} s"
    if t >= 1e6:
        return f"{t / 1e6:.3f} ms"
    if t >= 1e3:
        return f"{t / 1e3:.1f} us"
    return f"{t:.0f} ns"


def _fmt_n(n: float) -> str:
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e4:
        return f"{n / 1e3:.0f}k"
    return str(int(n))


def _rate(n: int, seconds: float) -> str:
    if seconds <= 0:
        return "?"
    r = n / seconds
    if r >= 1e6:
        return f"{r / 1e6:.2f} M/s"
    if r >= 1e3:
        return f"{r / 1e3:.1f} k/s"
    return f"{r:.1f} /s"


def _who(rep: "PinReport", rec: PinRecord | None, n: int = 3) -> str:
    if rec is None or not rec.data_pcs:
        return "?"
    names = [rep.where(p) for p in rec.data_pcs[:n]]
    return ", ".join(names) + (", ..." if len(rec.data_pcs) > n else "")


def _pl(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _period(times: list[float]) -> tuple[float, bool] | None:
    """Dominant full period (edge k -> edge k+2) and whether it is regular (>= 80 % within 10 %)."""
    if len(times) < 5:
        return None
    per = [times[k + 2] - times[k] for k in range(len(times) - 2)]
    med = statistics.median(per)
    if med <= 0:
        return None
    ok = sum(1 for p in per if abs(p - med) <= 0.1 * med)
    return med, ok >= 0.8 * len(per)


WAVE_EDGES = 65                 # edges looked at for the frequency / duty of a periodic pin


def wave_of(rec: PinRecord | None, now: float | None = None) -> dict | None:
    """{"edges", "freq_hz", "duty", "last_ns"} of a pin from its recent edges (any driver: PWM channel, GPIO, net),
    or None without edges. `edges` is exact (all level changes); frequency and duty come from log.wave_stats over the
    last WAVE_EDGES edges (median period, so a start-up glitch does not skew them); both None with fewer than three
    rising edges or once the pin has been still for more than 4 periods (`now`)."""
    if rec is None or not rec.level_events:
        return None
    ts, lv = rec.edge_times, rec.edge_levels
    n = min(len(ts), len(lv), WAVE_EDGES)
    out = {"edges": rec.level_events, "freq_hz": None, "duty": None, "last_ns": ts[-1] if ts else None}
    if n < 5:
        return out
    k0 = len(ts) - n
    e = [(ts[k0 + i], lv[len(lv) - n + i]) for i in range(n)]
    if sum(1 for _t, v in e if v == "H") < 3:
        return out
    from .log import wave_stats
    freq, duty = wave_stats(e)
    if freq is None:
        return out
    if now is not None and now - e[-1][0] > 4e9 / freq:
        return out                                      # stopped
    out["freq_hz"] = round(freq, 3)
    out["duty"] = None if duty is None else round(duty, 1)
    return out


def fmt_wave(w: dict | None) -> str:
    """'PWM 20.00 kHz, duty 25.0 %' for a periodic pin, '' otherwise."""
    if not w or not w.get("freq_hz"):
        return ""
    f = w["freq_hz"]
    fs = f"{f / 1e6:.4g} MHz" if f >= 1e6 else f"{f / 1e3:.4g} kHz" if f >= 1e3 else f"{f:.4g} Hz"
    d = w.get("duty")
    return f"periodic {fs}, duty " + (f"{d:.1f} %" if d is not None else "?")


def pin_waves(chip: Any) -> dict[str, dict]:
    """pin id -> wave_of(...) for every pin with level changes (for the web pin table)."""
    rep = _get_rep(chip)
    if rep is None:
        return {}
    now = rep.now()
    out = {}
    for pid, rec in rep.recs.items():
        w = wave_of(rec, now)
        if w is not None:
            out[pid] = w
    return out


def _static_pins(rep: Any, chip: Any = None) -> dict[str, list[str]]:
    """pin -> static notes predicted by firmware.analyze (PCR writes, PDDR output masks, PSOR/PCOR/PTOR masks)."""
    out: dict[str, list[str]] = {}
    if rep is None:
        return out
    board = getattr(chip, "pins", None)
    ids = {p.gpio_name for p in board.pins if p.gpio_name} if board is not None else None
    for r in rep.pin_roles:
        text = f"{r.pin} {r.note}"
        for p in dict.fromkeys(_PIN_RE.findall(text)):
            if ids is not None and p not in ids:
                continue
            note = f"{r.reg}{'=0x%X' % r.value if r.value is not None else ''} {r.note}".strip()
            lst = out.setdefault(p, [])
            if note not in lst:
                lst.append(note)
    return out


def _role(pin: Any, st: dict) -> str:
    own = pin.owner
    if own == "GPIO":
        if pin.direction == "out":
            s = "GPIO out"
            extra = []
            if pin.pull:
                extra.append(f"pull-{pin.pull}")
        else:
            s = "GPIO in"
            extra = [f"pull-{pin.pull}"] if pin.pull else []
        mode = st.get("irq_mode")
        if mode:
            extra.append(f"IRQ {mode.replace(' edge', '')}")
        return s + (f" ({', '.join(extra)})" if extra else "")
    if pin.direction == "analog" or own == "analog":
        return f"analog ({own})" if own not in ("analog", None) else "analog"
    if own in ("disabled", None):
        return "disabled"
    return str(own) + (f" (pull-{pin.pull})" if pin.pull else "")


def _periph_group(fn: str) -> tuple[str, str] | None:
    m = re.match(r"^([A-Z]+\d*)_(.+)$", fn)
    if m:
        return m.group(1), m.group(2)
    # TI C2000 signal names carry the instance letter/number without a separator: SCITXDA, SPISIMOA, CANRXB, EPWM1A
    m = re.match(r"^(SCI|SPI|CAN)([A-Z]+?)([A-D])$", fn)
    if m:
        return m.group(1) + m.group(3), m.group(2)
    m = re.match(r"^(EPWM\d+|ECAP\d+|EQEP\d+)([A-Z]+)$", fn)
    if m:
        return m.group(1), m.group(2)
    return None


def _c28x_live_pc(core: Any) -> int:
    """PC of the instruction executing right now: the pure-Python C28x core keeps it in a local of its run loop
    (`core.pc` is only written back at the end of a slice), so read it from that frame (as insns_in_slice does)."""
    import sys
    code = type(core).run.__code__
    f = sys._getframe(1)
    while f is not None:
        if f.f_code is code and f.f_locals.get("self") is core:
            return int(f.f_locals.get("pc", core.pc))
        f = f.f_back
    return core.pc


class _Row:
    __slots__ = ("pin", "rec", "st", "role", "cfg_t", "cfg_pc", "reads", "writes", "changes", "irqs", "notes",
                 "static", "reconf")


def _rows(chip: Any, rep: PinReport, stats: dict, spins: dict[str, list[str]] | None) -> list[_Row]:
    board = chip.pins
    now = rep.now()
    ids = set(rep.recs) | set(stats)
    rows = []
    for pid in ids:
        try:
            pin = board.get(pid)
        except KeyError:
            continue
        rec = rep.recs.get(pid)
        st = stats.get(pid, {})
        hist = rec.history if rec else []
        reads, writes = int(st.get("reads", 0)), int(st.get("writes", 0))
        changes, irqs = int(st.get("changes", 0)), int(st.get("irqs", 0))
        if rec is not None and pin.owner not in ("GPIO", "disabled") and pin.direction == "out":
            changes = max(changes, rec.level_events)     # driven by a peripheral (PWM / timer channel): PinBoard edges
        if not hist and not reads and not writes and not irqs and not st.get("pcr_writes"):
            continue                                    # only external stimulus / reset activity
        r = _Row()
        r.pin, r.rec, r.st = pin, rec, st
        r.role = _role(pin, st)
        last = rec.last if rec else None
        r.cfg_t = last[0] if last else None
        r.cfg_pc = last[1] if last else (rec.cfg_last_pc if rec else None)
        r.reads, r.writes, r.changes, r.irqs = reads, writes, changes, irqs
        cats = rec.cats if rec else {}
        again = {c: n - 1 for c, n in cats.items() if n > 1}
        r.reconf = sum(again.values())
        notes = []
        if st.get("floating"):
            notes.append("FLOATING when read")
        gpio = pin.owner == "GPIO"
        if gpio and pin.direction != "out" and not reads and not st.get("irq_mode") and not writes:
            notes.append("never read")
        if gpio and pin.direction == "out" and not writes and not changes:
            notes.append("never written")
        if gpio and pin.direction != "out" and writes:
            notes.append("output released to input between uses")
        if r.reconf:
            notes.append("reconfigured " + ", ".join(f"{c} {n}x" for c, n in again.items()))
        other = {f: n for f, n in (rec.funcs if rec else {}).items() if f not in (pin.owner, "GPIO" if gpio else "")}
        if other:
            notes.append("also " + ", ".join(f"{f} ({n}x)" for f, n in other.items()))
        wn = fmt_wave(wave_of(rec, now))
        if wn:
            notes.append(wn)
        r.notes = notes
        if spins is None:
            r.static = "-"
        else:
            r.static = "confirmed" if pid in spins else "not predicted"
        rows.append(r)
    rows.sort(key=lambda x: x.pin.num)
    return rows


def _get_rep(chip: Any) -> PinReport | None:
    return getattr(chip, "pin_report", None)


def format_pin_report(chip: Any, *, static: bool = True) -> str:
    """Per-pin table + discovery summary of everything the firmware did with the pins so far."""
    rep = _get_rep(chip)
    if rep is None:
        return "pin report: no pin activity recorder on this chip (no PinBoard / pin-mux model)"
    stats = rep.stats()
    sa = rep.static() if static else None
    spins = _static_pins(sa, chip) if sa is not None else None
    rows = _rows(chip, rep, stats, spins)
    now = rep.now()
    img = getattr(chip, "firmware", None)
    syms = rep.symbols
    lines = []
    src = getattr(img, "source", "") or ""
    how = (f"PCs resolved via {'ELF' if getattr(img, 'fmt', '') == 'elf' else 'image'} symbols" if syms.syms else
           "no symbols: sub_XXXX = heuristic function start, the nearest bl target / push {lr} below the PC")
    lines.append(f"Pin report after {_fmt_t(now)} of virtual time ({len(rows)} pins touched by firmware; {how})")
    if src:
        lines.append(f"firmware: {src}")
    lines.append("")
    hdr = (f"{'pin':<7} {'#':>3}  {'final role':<34} {'config@':>10}  {'configured by':<22} {'reads':>6} "
           f"{'writes':>6} {'chg':>6} {'irqs':>5}  {'static':<13} notes")
    lines.append(hdr)
    lines.append("-" * len(hdr))
    for r in rows:
        role = r.role if len(r.role) <= 34 else r.role[:33] + "~"
        by = rep.where(r.cfg_pc)
        by = by if len(by) <= 22 else by[:21] + "~"
        lines.append(f"{r.pin.id:<7} {r.pin.num:>3}  {role:<34} {_fmt_t(r.cfg_t):>10}  {by:<22} "
                     f"{_fmt_n(r.reads):>6} {_fmt_n(r.writes):>6} {_fmt_n(r.changes):>6} {_fmt_n(r.irqs):>5}  "
                     f"{r.static:<13} {'; '.join(r.notes)}".rstrip())
    lines.append("")
    lines.extend(_summary(chip, rep, rows, spins, now))
    return "\n".join(lines)


def _summary(chip: Any, rep: PinReport, rows: list[_Row], spins: dict | None, now: float) -> list[str]:
    out = ["Discovery summary"]

    def bullet(title: str, items: list[str]) -> None:
        if not items:
            return
        out.append(f"* {title}:")
        out.extend(f"    - {x}" for x in items)

    outs = []
    for r in rows:
        if r.pin.owner == "GPIO" and (r.changes or r.writes):
            rec = r.rec
            per = _period(list(rec.edge_times)) if rec else None
            ps = ""
            if per:
                med, regular = per
                ps = (f", period {_fmt_t(med)} ({1e9 / med:.4g} Hz)" if regular
                      else f", irregular (median period {_fmt_t(med)})")
            who = _who(rep, rec)
            rel = "" if r.pin.direction == "out" else " (released to input between uses, e.g. a chip select)"
            outs.append(f"{r.pin.id}: {_pl(r.changes, 'level change')} from {_pl(r.writes, 'write')}{ps}; "
                        f"driven by {who}{rel}"
                        + (f", now {r.pin.level()}" if r.pin.level() else ""))
    bullet("Outputs the firmware drives", outs)
    pwm = []
    for r in rows:
        if r.pin.owner not in ("GPIO", "disabled") and r.pin.direction == "out" and r.changes:
            wn = fmt_wave(wave_of(r.rec, now))
            pwm.append(f"{r.pin.id}: {r.pin.owner}, {_pl(r.changes, 'level change')}" + (f", {wn}" if wn else "")
                       + f", now {r.pin.level()}")
    bullet("Outputs driven by peripherals (PWM / timer channels)", pwm)

    polled = []
    irq_in = []
    for r in rows:
        if r.pin.owner != "GPIO" or r.pin.direction == "out":
            continue
        rec = r.rec
        if r.writes and not r.reads and not r.st.get("irq_mode"):
            continue
        if r.reads:
            span = (now - (r.cfg_t or 0.0)) / 1e9
            who = _who(rep, rec)
            polled.append(f"{r.pin.id}: {r.reads} PDIR reads (~{_rate(r.reads, span)}) by {who}"
                          + (f", pull-{r.pin.pull}" if r.pin.pull else ", no pull"))
        mode = r.st.get("irq_mode")
        if mode:
            isr = rep.where(rec.isr_pc) if rec is not None and rec.isr_pc is not None else None
            irq_in.append(f"{r.pin.id}: IRQ {mode}, {r.irqs} interrupt flag{'s' if r.irqs != 1 else ''} set"
                          + (f", cleared by {isr}" if isr else "")
                          + (" (level reads X: counts as 0)" if r.st.get("floating") and "logic" in mode else ""))
    bullet("Inputs it polls", polled)
    bullet("Inputs with interrupts", irq_in)

    groups: dict[str, list[str]] = {}
    other_fn = []
    for r in rows:
        fns = [str(r.pin.owner)] if r.pin.owner not in ("GPIO", "disabled", None) and r.pin.direction != "analog" \
            else []
        if r.rec is not None:
            fns += [f for f in r.rec.funcs if f not in fns and f not in ("GPIO", "disabled", "?")
                    and not f.startswith(("ADC", "CMP", "DAC")) and f != r.pin.owner]
        for k, own in enumerate(fns):
            g = _periph_group(own)
            cur = "" if own == r.pin.owner else (
                f" (muxed {r.rec.funcs.get(own, 1)}x, now {r.pin.owner})" if r.rec is not None else "")
            if g:
                groups.setdefault(g[0], []).append(f"{r.pin.id} {g[1]}{cur}")
            else:
                other_fn.append(f"{r.pin.id} {own}{cur}")
    per_items = [f"{k}: {', '.join(v)}" for k, v in sorted(groups.items())]
    if other_fn:
        per_items.append("other: " + ", ".join(other_fn))
    analog = [f"{r.pin.id} ({r.pin.owner})" for r in rows if r.pin.direction == "analog"
              and not any(_periph_group(f) and f not in ("GPIO", "disabled") for f in (r.rec.funcs if r.rec else {})
                          if f != r.pin.owner)]
    if analog:
        per_items.append("analog: " + ", ".join(analog))
    bullet("Peripheral-owned pins", per_items)

    unused = [f"{r.pin.id} ({r.role}; {', '.join(n for n in r.notes if n.startswith('never')) or 'no traffic'})"
              for r in rows if r.pin.owner == "GPIO" and not r.reads and not r.writes and not r.changes
              and not r.st.get("irq_mode")]
    unused += [f"{r.pin.id} (disabled after configuration)" for r in rows if r.pin.owner == "disabled"
               and not any(f not in ("disabled", "?") for f in (r.rec.funcs if r.rec else {}))]
    bullet("Configured but unused so far", unused)

    flo = [f"{r.pin.id}: sampled while floating (no pull, nothing connected; PDIR and the interrupt logic see 0); "
           f"try --set {r.pin.id}=1 or --set {r.pin.id}=0" for r in rows if r.st.get("floating")]
    bullet("Floating inputs", flo)

    fn_pins: dict[str, list[str]] = {}
    for r in rows:
        if r.rec is None:
            continue
        for _, pc, _w in r.rec.history:      # (kept entries; enough to name the configuring functions)
            f = rep.symbols.function_of(pc) if pc is not None else None
            if f:
                lst = fn_pins.setdefault(f, [])
                if r.pin.id not in lst:
                    lst.append(r.pin.id)
    if fn_pins:
        bullet("Configuration code", [f"{f}: {len(p)} pin{'s' if len(p) != 1 else ''} ({', '.join(p)})"
                                      for f, p in sorted(fn_pins.items(), key=lambda kv: -len(kv[1]))])

    if spins is not None:
        dyn = {r.pin.id for r in rows}
        conf = sorted((p for p in dyn if p in spins), key=_sortkey)
        notp = sorted((p for p in dyn if p not in spins), key=_sortkey)
        never = sorted((p for p in spins if p not in dyn), key=_sortkey)
        items = [f"confirmed static guess: {len(conf)}" + (f" ({', '.join(conf)})" if conf else ""),
                 f"not predicted statically: {len(notp)}" + (f" ({', '.join(notp)})" if notp else "")
                 + (" - pins reached through tables/function arguments defeat the static scan" if notp else ""),
                 f"predicted but never configured: {len(never)}" + (f" ({', '.join(never)})" if never else "")
                 + (" - code not exercised in this run, or a misread store" if never else "")]
        bullet("Static analysis (chipem info) vs. this run", items)
    out.append("* Only code paths that ran are covered: drive inputs (--set/--at), feed UARTs (--uart-in) or run "
               "longer to exercise more of the firmware.")
    return out


def _sortkey(p: str) -> tuple:
    m = re.match(r"^\D*?([A-Z])(\d+)$", p)              # "PTC5" / "PA5": (port letter, bit)
    return (m.group(1), int(m.group(2))) if m else (p, 0)


SHOW = 12


def pin_history(chip: Any, name: str, *, full: bool = False) -> str:
    """Configuration history and counters of one pin."""
    rep = _get_rep(chip)
    if rep is None:
        return "no pin activity recorder on this chip"
    pin = chip.pins.get(name)
    rec = rep.recs.get(pin.id)
    st = rep.stats().get(pin.id, {})
    lines = [f"{pin.id}: {_role(pin, st)}  reads={st.get('reads', 0)} writes={st.get('writes', 0)} "
             f"level-changes={st.get('changes', 0)} irqs={st.get('irqs', 0)}"
             + ("  FLOATING when read" if st.get("floating") else "")]
    if rec is None or not rec.history:
        lines.append("  (no configuration by firmware recorded)")
    else:
        hist = rec.history
        cut = rec.omitted
        if not full and len(hist) > 2 * SHOW:
            cut += len(hist) - 2 * SHOW
            hist = hist[:SHOW] + [None] + hist[-SHOW:]
        elif cut:
            hist = rec.head + [None] + list(rec.tail)
        for e in hist:
            if e is None:
                lines.append(f"  {'':>12}  ... {cut} entries not shown ...")
                continue
            t, pc, what = e
            lines.append(f"  {_fmt_t(t):>12}  {rep.where(pc):<28} {what}")
    if rec is not None:
        if rec.cfg_first_pc is not None:
            lines.append(f"  control registers (PCR/PDDR): first {rep.where(rec.cfg_first_pc)}, last {rep.where(rec.cfg_last_pc)}")
        if rec.data_first_pc is not None:
            lines.append(f"  data registers: first {rep.where(rec.data_first_pc)}, last {rep.where(rec.data_last_pc)}"
                         f"; seen at {', '.join(rep.where(p) for p in rec.data_pcs)}")
        if rec.isr_pc is not None:
            lines.append(f"  interrupt flag cleared by {rep.where(rec.isr_pc)}")
        per = _period(list(rec.edge_times))
        if per:
            lines.append(f"  level-change period {_fmt_t(per[0])} ({'regular' if per[1] else 'irregular'})")
    return "\n".join(lines)


def report_dict(chip: Any, *, static: bool = True) -> dict:
    """JSON-ready report for the web UI."""
    rep = _get_rep(chip)
    if rep is None:
        return {"text": format_pin_report(chip), "pins": []}
    stats = rep.stats()
    sa = rep.static() if static else None
    spins = _static_pins(sa, chip) if sa is not None else None
    rows = _rows(chip, rep, stats, spins)
    pins = []
    for r in rows:
        pins.append({"pin": r.pin.id, "num": r.pin.num, "role": r.role, "cfg_t": r.cfg_t,
                     "cfg_by": rep.where(r.cfg_pc), "reads": r.reads, "writes": r.writes, "changes": r.changes,
                     "irqs": r.irqs, "static": r.static, "notes": r.notes,
                     "history": [{"t": t, "by": rep.where(pc), "what": w} for t, pc, w in (r.rec.history if r.rec else [])]})
    return {"text": format_pin_report(chip, static=static), "pins": pins}


def configured_by(chip: Any) -> dict[str, dict]:
    """pin id -> {"cfg_by": "main+0x1c", "cfg_t": t_ns} of the last configuration (cheap; for tooltips)."""
    rep = _get_rep(chip)
    out: dict[str, dict] = {}
    if rep is None:
        return out
    for pid, rec in rep.recs.items():
        last = rec.last
        if last is not None:
            t, pc, what = last
            out[pid] = {"cfg_by": rep.where(pc), "cfg_t": t, "cfg_what": what}
    return out
