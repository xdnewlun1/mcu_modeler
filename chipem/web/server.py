"""Local web UI for a Session: emulator worker thread + ThreadingHTTPServer (stdlib only).

Threading model
---------------
Session/Chip are not thread-safe, so *every* chip access happens on one `Worker` thread:
  * HTTP handlers never touch the chip.  Commands (`run`, `pause`, `pin`, ...) are put on the worker's queue as
    closures together with a `concurrent.futures.Future`; the handler waits (bounded) for the result.
  * While "running", the worker executes `session.run_for(chunk)` in small virtual-time chunks (default 5 ms)
    and drains the command queue between chunks, so commands are served within one chunk's wall time.
    `pause` additionally calls `session.stop()` (documented thread-safe) to cut a long chunk short.
  * After every chunk/command the worker publishes immutable snapshots (state + pin table) and appends new log
    events (as JSON-ready dicts) to a bounded buffer (`EventBuffer`: MAX_EVENTS, of which at most MAX_PIN_EVENTS
    `pin` events, so PWM edges never evict reset/info/uart/warn events; the session log itself stores a fast PWM
    pin as a burst of edges and then `wave` summaries, see chipem/log.py). Pin rows carry `wave`
    ({edges, freq_hz, duty}) for pins with level changes (pinreport.pin_waves).  Read-only endpoints (`/api/state`, `/api/events`,
    `/api/stream`) are served from those snapshots, so they never block on a busy worker.
  * A `threading.Condition` wakes SSE streams when something new is published.
  * Board wiring (nets, links) can change at runtime (`/api/net`, `/api/link`, auto UART links when firmware
    re-muxes pins). The worker re-derives the wiring part of `/api/chip` whenever `Board.wiring` changes, and
    `state.board.wiring` tells clients to refetch it.

Stream payload (SSE `state` frames; /api/state keeps the full pin rows plus `waves` for compatibility):
  pins       dynamic pin fields only (SLIM_KEYS: chip, num, level, owner, direction, drive, pull, external, net,
             cfg_by, cfg_t, cfg_what; the static ones are in /api/chip). `pins_full: true` = the complete set (first
             frame of a stream, after a reset or session swap); otherwise only the rows that changed since this
             stream's previous frame (merge by chip + num). No `pins` key = nothing changed.
  waves      {"A:77": {edges, freq_hz, duty %}} of periodic pins, at most every WAVES_INTERVAL_S and only when changed;
             not part of the pin change detection (edge counts change all the time).
  adc        {chip: {channel: value}} analog inputs set through /api/adc / --adc (int raw code, float volts).
  periph     [{chip, block, type: can|eth|usb|sd, ...}] bus peripheral status (periph_status), every PERIPH_INTERVAL_S.
  board.buses [{name, type, stats}] live counters of /api/chip `buses`.

Static description:
  GET  /api/chip                   chips, pins (each with `adc`: the /api/adc channel of an analog input, or null),
                                   board, nets, links (UART), parts and `buses` (CAN / Ethernet / USB host links:
                                   {type, name, nodes, host, describe, stats}; YAML export unchanged)

Control:
  POST /api/step {n?, chip?}       n instructions on one chip (default the view chip; the others do not move)
  POST /api/restart                rebuild the current setup with virtual time 0 (page reloads on the new boot id)
  POST /api/stimulus {spec}        Session.apply_stimulus: "10ms:PTC6=0", "PTC6=H", "1ms:UART0=hi\\r\\n", "SW1=H";
                                   "+5ms:B.PA0=H" = 5 ms from now. Returns {t_ns (absolute, null = applied now), spec}

Data extraction:
  GET  /api/vcd?chip=A,B&pins=A.PTC6,B.PA0   VCD of the kept `pin` level events (attachment); dense PWM stored as
                                   `wave` summaries is not expanded
  GET  /api/uart/lines             transcript of every UART from the kept uart events {lines: [{uart, chip, t, text,
                                   open}], after_cr, drops, last} (same line rules as app.js; continue the stream
                                   from `last`)
  GET  /api/events?since=&kinds=pin,uart&chip=A,board&q=text|re:regex&pin=A.PTC6&limit=&format=json|txt|csv
                                   filtered event log (txt/csv: attachments, limit 0 = all kept events).
                                   Every event carries `desc`, its text as the UI shows it (web/evfmt.py); `q`
                                   matches the row `time \t [chip \t] kind \t desc` exactly as the browser's
                                   filter does, and `.txt` is those rows.
  GET  /api/uart?uart=X&since=N&format=json|txt   output after byte offset N (txt: attachment)
  GET  /api/hostserial             host serial bridges {realtime: {enabled, speed, lag_ms, max_lag_ms, ratio, ...},
                                   endpoints: [{name, transport, path, connected, line, bytes_in, bytes_out,
                                   dropped, dropped_out, ...}]} (chipem/hostserial.py; also state.host)
  POST /api/realtime {on, speed?}  real-time pacing on/off and speed factor (Session.set_realtime)
  POST /api/adc {channel, value}   analog input: JSON int / "2048" = raw code; JSON float / "1.65V" / "1200mV" = volts
                                   (session.parse_adc_value; volts only for `adc_volts` models)

Board wiring API (board sessions only; all executed on the worker):
  GET  /api/board                  {name, quantum_ns, wiring, chips: [{id, name, uarts}], nets, links}
  GET  /api/board/yaml             the current board as board YAML (?download=1: as an attachment)
  POST /api/net                    {pins: ["A.PTC6", "B.PA0", ...], name?, pull?} -> new net, or pins added to
                                   the net they touch (several nets touched: merged)
  POST /api/net/rename             {name, new}
  POST /api/net/remove             {name}      (or DELETE /api/net/<name>)
  POST /api/link                   {from: "A.UART0", to: "B.USART1", bidir: true}
  POST /api/link/remove            {from, to} or {index}   (or DELETE /api/link?from=..&to=..)

Board parts (switches, LEDs, gates, ...; chipem/parts; board sessions only):
  GET  /api/parts/types            [{type, params: {name: default}, ports: {name: dir}}] (any session)
  POST /api/part                   {id?, type, params, ports: {port: net | "CHIP.PIN"}} (id auto-assigned if absent)
  POST /api/part/set               {id, values: {...}}     user action (switch level, pressed, period, ...)
  POST /api/part/remove            {id}
  POST /api/part/port              {id, port, to: net | "CHIP.PIN" | null}   re-wire / disconnect one port
  `parts` ([{id, type, params, ports: {port: net}, dirs: {port: dir}, state}]) is part of /api/board and /api/chip;
  live part states are in state.board.parts [{id, state, ...}]; `part` log events go out on the SSE stream.
"""
from __future__ import annotations

import json
import math
import re
import mimetypes
import uuid
import queue
import threading
import time
import traceback
from collections import deque
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from ..cmd_shell import event_dict, jsonable
from .evfmt import describe_event, event_line, text_matcher

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_EVENTS = 20000          # events kept for /api/events and the SSE stream ...
MAX_PIN_EVENTS = 12000      # ... of which at most this many `pin` events (PWM must not evict reset/info/uart/warn)
MAX_EVENTS_PER_POLL = 4000  # if the chip logs more than this per chunk, only the newest are published
MAX_STEPS = 1_000_000       # /api/step n limit (single steps run at ~1e5/s; use /api/run for more)
BODY_TIMEOUT_S = 10.0       # a request body that does not arrive within this is answered 408


class EventBuffer:
    """The web's bounded event list with a per-kind cap: `pin` events (PWM edges, waveform summaries) are kept in
    their own ring of `pin_cap` and every other kind in a ring of `total - pin_cap`, so a fast PWM can never push
    reset / info / uart / warn events out. Iteration merges both rings in sequence (`seq`) order."""

    def __init__(self, total: int = MAX_EVENTS, pin_cap: int = MAX_PIN_EVENTS):
        self.pin: deque[dict] = deque(maxlen=pin_cap)
        self.other: deque[dict] = deque(maxlen=max(1, total - pin_cap))

    def append(self, d: dict) -> None:
        (self.pin if d.get("kind") == "pin" else self.other).append(d)

    def clear(self) -> None:
        self.pin.clear()
        self.other.clear()

    def __len__(self) -> int:
        return len(self.pin) + len(self.other)

    def __iter__(self):
        import heapq
        return heapq.merge(self.pin, self.other, key=lambda e: e.get("seq", 0))


class Worker:
    """Owns the Session; runs it in chunks and executes queued commands. All chip access happens here."""

    def __init__(self, session: Any, *, chunk_ns: float = 5e6, realtime: bool = False,
                 publish_interval: float = 0.03):
        self.s = session
        self.chunk_ns = float(chunk_ns)
        self.realtime = realtime
        self.publish_interval = publish_interval
        self.q: queue.Queue[tuple[Callable[[], Any], Future]] = queue.Queue()
        self.cond = threading.Condition()
        self.running = False            # free-running or running toward run_target_ns
        self.run_target_ns: float | None = None
        self.last_stop: str | None = None
        self.error: str | None = None
        self.snapshot: dict = {}
        self.version = 0
        self.events = EventBuffer()
        self.event_seq = 0              # seq of the newest published event
        self._cmd_gen = 0               # bumped by pause(): queued run/step commands from before it are void
        self._last_log_seq = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="chipem-worker", daemon=True)
        self._rt_anchor: tuple[float, float] | None = None
        self._last_pub = 0.0
        self._last_pins: list = []
        self.pins_version = 0
        self._uart_names: list[str] | None = None
        self._wiring_seen: int | None = None
        self.wiring_info: dict = {}         # board_info() of the current wiring (re-derived when it changes)
        self._log_obj: Any = None           # the session's log; a single chip that gets parts switches to a board log
        self._seq = 0                       # our own event numbering (stable across that switch)
        self._had_board = _is_board(session)
        self.full_info: dict | None = None  # chip_info() re-derived when a lone chip became a one-chip board
        self.pins_epoch = 0                 # bumped on reset / session swap: streams resend the full pin set
        self._slim: dict = {}               # (chip, num) -> slim row (pins_version bumps when one changes)
        self._waves: dict = {}              # "A:77" -> {edges, freq_hz, duty}
        self.waves_version = 0
        self._periph: list = []
        self._periph_t = -1e9
        self._publish(force=True)

    # ---- lifecycle ---------------------------------------------------------------------------------------
    def start(self) -> None:
        self._thread.start()

    def shutdown(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self.running = False
        try:
            self.s.stop()
        except Exception:
            pass
        self.q.put((lambda: None, Future()))
        self._thread.join(timeout)
        with self.cond:
            self.cond.notify_all()

    @property
    def alive(self) -> bool:
        return not self._stop.is_set()

    # ---- command interface (called from HTTP threads) ----------------------------------------------------
    def call(self, fn: Callable[[], Any], timeout: float = 10.0) -> Any:
        fut: Future = Future()
        self.q.put((fn, fut))
        return fut.result(timeout)

    def pause(self) -> None:
        """Thread-safe: stop free-running immediately (cuts the current chunk short). A run or step command queued
        before the pause (still waiting behind a busy worker) is cancelled, and a step loop in progress ends."""
        self._cmd_gen += 1
        self.running = False
        self.run_target_ns = None
        try:
            self.s.stop()
        except Exception:
            pass

    # ---- worker thread -----------------------------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            item = None
            try:
                item = self.q.get_nowait() if self.running else self.q.get(timeout=0.25)
            except queue.Empty:
                pass
            while item is not None:
                fn, fut = item
                if fut.set_running_or_notify_cancel():
                    try:
                        res = fn()
                    except BaseException as e:      # noqa: BLE001 - reported to the caller
                        self._publish(force=True)
                        fut.set_exception(e)
                    else:
                        self._publish(force=True)   # publish before completing: callers then see fresh state
                        fut.set_result(res)
                try:
                    item = self.q.get_nowait()
                except queue.Empty:
                    item = None
            if self._stop.is_set():
                break
            if self.running:
                try:
                    self._chunk()
                except Exception as e:  # noqa: BLE001 - keep the worker alive; report through the state
                    self.running = False
                    self.error = f"{type(e).__name__}: {e}"
                    traceback.print_exc()
        # fail anything still queued
        while True:
            try:
                _, fut = self.q.get_nowait()
            except queue.Empty:
                break
            if fut.set_running_or_notify_cancel():
                fut.set_exception(RuntimeError("emulator stopped"))

    def _chunk(self) -> None:
        s = self.s
        now = s.chip.clock.now_ns()
        n = self.chunk_ns
        tgt = self.run_target_ns              # read once: pause() may clear it from an HTTP thread
        if tgt is not None:
            n = min(n, tgt - now)
            if n <= 0:
                self.running = False
                self.run_target_ns = None
                self.last_stop = "DONE"
                self._publish(force=True)
                return
        if self.realtime and getattr(s, "pacer", None) is None:     # (a pacing session paces itself)
            wall = time.monotonic()
            if self._rt_anchor is None:
                self._rt_anchor = (wall, now)
            w0, v0 = self._rt_anchor
            ahead = (now - v0) / 1e9 - (wall - w0)
            if ahead > 0:
                time.sleep(min(ahead, 0.05))
                return
            if ahead < -0.5:                  # fell far behind (slow emulation): re-anchor, don't burst
                self._rt_anchor = (wall, now)
        try:
            r = s.run_for(n)
        except Exception as e:
            self.running = False
            self.error = f"{type(e).__name__}: {e}"
            traceback.print_exc()
            self._publish(force=True)
            return
        name = getattr(r, "name", str(r))
        if name not in ("COUNT",):
            if name == "USER" and self.running:
                pass                          # stop() raced with a command; keep going
            else:
                self.running = False
                self.run_target_ns = None
                self.last_stop = name
        tgt = self.run_target_ns
        if tgt is not None and s.chip.clock.now_ns() >= tgt - 0.5:
            self.running = False
            self.run_target_ns = None
            self.last_stop = "DONE"
        self._publish(force=not self.running)

    def _publish(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_pub < self.publish_interval:
            return
        self._last_pub = now
        s = self.s
        try:
            b = getattr(s, "board", None)
            if (b is not None) != self._had_board:          # first part on a single chip: now a one-chip board
                self._had_board = b is not None
                self._uart_names = None
                self.full_info = chip_info(s)
            log = getattr(s, "log", None)
            if log is not self._log_obj:
                # the single chip's log became a board's merged log, which starts with a copy of the chip's events:
                # continue after the copy of the last event already published (not from 0, which re-sent them all)
                done = self._last_log_seq if self._log_obj is not None else 0
                self._log_obj = log
                self._last_log_seq = max([new for _cid, old, new in getattr(b, "backlog_seqs", ()) if old <= done],
                                         default=0) if done else 0
            st = jsonable(s.state())
            pins = jsonable(all_pins(s))
            new = s.events(self._last_log_seq)
            if b is not None and getattr(b, "wiring", 0) != self._wiring_seen:
                self.wiring_info = board_info(s)
                self._wiring_seen = getattr(b, "wiring", 0)
        except Exception as e:                # never kill the worker over a snapshot
            self.error = f"snapshot failed: {type(e).__name__}: {e}"
            return
        if new:
            # a board session returns its events sorted by time; keep the buffer in sequence order so that
            # "seq > since" polling stays exact
            new = sorted(new, key=lambda e: getattr(e, "seq", 0))
            self._last_log_seq = max(self._last_log_seq, getattr(new[-1], "seq", self._last_log_seq + len(new)))
            if len(new) > MAX_EVENTS_PER_POLL:          # keep every non-pin event; drop the oldest pin events
                other = [e for e in new if getattr(e, "kind", None) != "pin"][-MAX_EVENTS_PER_POLL:]
                room = MAX_EVENTS_PER_POLL - len(other)
                pins = [e for e in new if getattr(e, "kind", None) == "pin"][-room:] if room > 0 else []
                new = sorted(other + pins, key=lambda e: getattr(e, "seq", 0))
        if b is not None and isinstance(st.get("board"), dict) and "parts" not in st["board"]:
            try:
                st["board"]["parts"] = [{"id": k, "state": v} for k, v in jsonable(part_states(b)).items()]
            except Exception:
                pass
        st["running"] = self.running
        st["run_target_ns"] = self.run_target_ns
        st["last_stop"] = self.last_stop
        st["error"] = self.error
        st["realtime"] = self.realtime or getattr(s, "pacer", None) is not None
        if hasattr(s, "host_status"):
            try:
                st["host"] = jsonable(s.host_status())     # host serial bridges + real-time lag (/api/hostserial)
            except Exception:
                pass
        try:
            st["uart_out_len"] = {u: len(s.uart_output(u)) for u in self._uarts()}
        except Exception:
            pass
        slim = {(r.get("chip"), r.get("num")): slim_row(r) for r in pins}
        if slim != self._slim:
            self._slim = slim
            self.pins_version += 1
        self._last_pins = pins
        waves = {f"{r.get('chip')}:{r.get('num')}": r["wave"] for r in pins if r.get("wave")}
        if waves != self._waves:
            self._waves = waves
            self.waves_version += 1
        try:
            st["adc"] = jsonable(adc_values(s))
        except Exception:
            st["adc"] = {}
        if now - self._periph_t >= PERIPH_INTERVAL_S or force:
            self._periph_t = now
            try:
                self._periph = periph_status(s)
            except Exception:
                self._periph = []
        st["periph"] = self._periph
        if b is not None and isinstance(st.get("board"), dict):
            try:
                st["board"]["buses"] = jsonable(bus_stats(b))
            except Exception:
                st["board"]["buses"] = []
        with self.cond:
            for ev in new:
                d = event_dict(ev)
                d["desc"] = describe_event(d)          # the UI's text for this event (one formatter: evfmt.py)
                self._seq += 1
                d["seq"] = self._seq
                self.events.append(d)
            self.event_seq = self._seq
            self.version += 1
            self.snapshot = {"state": st, "pins": self._last_pins, "pins_version": self.pins_version,
                             "version": self.version, "event_seq": self.event_seq, "slim": self._slim,
                             "pins_epoch": self.pins_epoch, "waves": self._waves,
                             "waves_version": self.waves_version}
            self.cond.notify_all()

    def _uarts(self) -> list[str]:
        if self._uart_names is None:
            self._uart_names = [q for _cid, _name, q in uart_names(self.s)]
        return self._uart_names

    # ---- helpers for HTTP threads ------------------------------------------------------------------------
    def events_page(self, since: int, kinds: set[str] | None = None, limit: int = 2000,
                    pred: Callable[[dict], bool] | None = None) -> tuple[list[dict], int]:
        """(events_since(...), seq to resume from): both read under the same lock, so an event published in
        between is neither lost nor counted twice."""
        with self.cond:
            evs = self.events_since(since, kinds, limit, pred)
            last = evs[-1].get("seq", self.event_seq) if limit and len(evs) == limit else self.event_seq
        return evs, last

    def events_since(self, since: int, kinds: set[str] | None = None, limit: int = 2000,
                     pred: Callable[[dict], bool] | None = None) -> list[dict]:
        with self.cond:
            evs = [e for e in self.events if e.get("seq", 0) > since and (kinds is None or e.get("kind") in kinds)
                   and (pred is None or pred(e))]
        return evs[:limit] if limit else evs      # oldest first: callers resume from the last one returned

    # ---- commands (closures executed on the worker) ------------------------------------------------------
    def cmd_run(self, ms: float | None) -> dict:
        gen = self._cmd_gen

        def fn():
            if self._cmd_gen != gen:              # paused after this run was requested
                return {"running": False, "cancelled": True}
            self.error = None
            self.last_stop = None
            self._rt_anchor = None
            self.run_target_ns = None if ms is None else self.s.chip.clock.now_ns() + float(ms) * 1e6
            self.running = True
            return {"running": True, "until_ns": self.run_target_ns}
        return self.call(fn)

    def cmd_step(self, n: int, chip: str | None = None) -> dict:
        """n instructions on chip `chip` (default: the view chip; the other board chips do not move)."""
        n = int(n)
        if not 1 <= n <= MAX_STEPS:
            raise ValueError(f"n must be 1..{MAX_STEPS} (use /api/run for longer)")
        gen = self._cmd_gen

        def fn():
            ctx = _ctx(self.s, chip)                  # ValueError for an unknown chip id
            cid = getattr(ctx, "id", None) or _chip_ids(self.s)[0]
            self.running = False
            self.run_target_ns = None
            r = None
            for _ in range(n):
                if self._cmd_gen != gen:          # pause/reset requested meanwhile
                    break
                r = self.s.step(chip=cid) if chip is not None and hasattr(self.s, "ctxs") else self.s.step()
                if getattr(r, "name", "") == "FAULT":
                    break
            self.last_stop = "STEP"
            return {"stop": getattr(r, "name", str(r)), "pc": ctx.chip.core.pc, "chip": cid}
        return self.call(fn)

    def replace_session(self, new: Any, on_swap: Callable[[], None] | None = None) -> Any:
        """Board editor: run `new` instead of the current session (structural change: MCUs added/removed, firmware
        changed, other board opened). Returns the old session (the caller closes it). Event numbering continues;
        the event buffer starts empty for the new setup."""
        def fn():
            self._cmd_gen += 1
            self.running = False
            self.run_target_ns = None
            old, self.s = self.s, new
            self._log_obj = None
            self._last_log_seq = 0
            self._had_board = _is_board(new)
            self.full_info = None
            self._uart_names = None
            self._wiring_seen = None
            self.wiring_info = {}
            self._last_pins = []
            self._slim = {}
            self.pins_epoch += 1
            self.last_stop = "RESET"
            self.error = None
            with self.cond:
                self.events.clear()
            if on_swap is not None:
                on_swap()
            self._publish(force=True)
            return old
        return self.call(fn, timeout=30)

    def cmd_reset(self) -> dict:
        def fn():
            self.running = False
            self.run_target_ns = None
            self.s.reset()
            self.last_stop = "RESET"
            self.error = None
            self.pins_epoch += 1
            return {"pc": self.s.chip.core.pc}
        return self.call(fn)


# ---------------------------------------------------------------------------------------------------------------

def event_filter(chips: str, q: str, board: bool = False, default_chip: str = "A") -> Callable[[dict], bool] | None:
    """Predicate for /api/events: `chip` = comma list of chip ids ("board" for board-level events), `q` = the
    browser's filter (evfmt.text_matcher: case-insensitive substring, or `re:` regex with smart case) applied to the
    row text the browser shows (evfmt.event_line)."""
    cs = {c for c in (chips or "").split(",") if c}
    m = text_matcher(q or "")
    if not cs and m is None:
        return None

    def pred(e: dict) -> bool:
        if cs and str(e.get("chip", default_chip)) not in cs:
            return False
        return m is None or m(event_line(e, board, default_chip))
    return pred


def _event_detail(e: dict) -> str:
    return " ".join(f"{k}={v}" for k, v in e.items() if k not in ("seq", "t_ns", "kind", "chip", "desc"))


def events_text(evs: list[dict], board: bool = False, default_chip: str = "A") -> str:
    """Plain-text export: the rows as the UI shows (and copies) them, `time \t [chip \t] kind \t text`."""
    lines = [event_line(e, board, default_chip) for e in evs]
    return "\n".join(lines) + ("\n" if lines else "")


def events_csv(evs: list[dict]) -> str:
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["seq", "t_ns", "chip", "kind", "text", "detail"])
    for e in evs:
        w.writerow([e.get("seq", ""), e.get("t_ns", ""), e.get("chip", ""), e.get("kind", ""),
                    e.get("desc", ""), _event_detail(e)])
    return buf.getvalue()


def _pin_keys(spec: str, board: bool, chip0: str) -> set[tuple[str, str]]:
    """'A.PTC6,B.PA0' -> {("A", "PTC6"), ("B", "PA0")} (unqualified names: chip `chip0`)."""
    out = set()
    for item in (spec or "").split(","):
        item = item.strip()
        if not item:
            continue
        cid, _, name = item.partition(".")
        if not name:
            cid, name = chip0, item
        out.add((cid.upper(), name.upper()))
    return out


def pin_filter(spec: str, board: bool, chip0: str) -> Callable[[dict], bool] | None:
    """Predicate for /api/events?pin=: events about exactly these pins (any field)."""
    keys = _pin_keys(spec, board, chip0)
    if not keys:
        return None

    def pred(e: dict) -> bool:
        if e.get("kind") != "pin":
            return False
        cid = str(e.get("chip", chip0)).upper()
        p = str(e.get("pin", "")).upper()
        if p.startswith(cid + "."):
            p = p[len(cid) + 1:]
        return (cid, p) in keys
    return pred


def _and(*preds):
    ps = [p for p in preds if p is not None]
    if not ps:
        return None
    if len(ps) == 1:
        return ps[0]
    return lambda e: all(p(e) for p in ps)


_VCD_VAL = {"H": "1", "L": "0", "Z": "z", True: "1", False: "0", 1: "1", 0: "0"}


def events_vcd(evs: list[dict], name: str, board: bool, chip0: str, want: set | None = None) -> str:
    """A VCD of the `pin` level events in `evs` (timescale 1 ns). Dense PWM edges that the event log keeps only as
    `wave` summaries are not expanded (noted in the header); the initial value of a signal is the `old` level of its
    first event."""
    sig: dict[tuple[str, str], list] = {}
    for e in evs:
        if e.get("kind") != "pin" or (e.get("field") or "level") != "level":
            continue
        cid = str(e.get("chip", chip0))
        p = str(e.get("pin", ""))
        if p.upper().startswith(cid.upper() + "."):
            p = p[len(cid) + 1:]
        if want is not None and (cid.upper(), p.upper()) not in want:
            continue
        sig.setdefault((cid, p), []).append(e)
    if want is not None:                       # requested pins without events still get a (constant x) trace
        for cid, p in want:
            if not any(k[0].upper() == cid and k[1].upper() == p for k in sig):
                sig[(cid, p)] = []
    ids = {}
    for i, k in enumerate(sorted(sig, key=lambda k: (k[0], k[1]))):
        code, n = "", i
        while True:
            code += chr(33 + n % 94)
            n //= 94
            if not n:
                break
        ids[k] = code
    lines = ["$date chipem web export $end", "$version chipem $end", "$timescale 1ns $end",
             "$comment level changes kept by the web server's event list; dense PWM stored as `wave` summaries "
             "(chipem/log.py) is not expanded: use `chipem run --vcd` for every edge $end",
             f"$scope module {re.sub(r'[^A-Za-z0-9_]', '_', name) or 'chipem'} $end"]
    by_chip: dict[str, list] = {}
    for k in ids:
        by_chip.setdefault(k[0], []).append(k)
    for cid, ks in by_chip.items():
        if board:
            lines.append(f"$scope module {cid} $end")
        for k in ks:
            lines.append(f"$var wire 1 {ids[k]} {re.sub(r'[^A-Za-z0-9_.]', '_', k[1])} $end")
        if board:
            lines.append("$upscope $end")
    lines += ["$upscope $end", "$enddefinitions $end", "#0", "$dumpvars"]
    for k, code in ids.items():
        first = sig[k][0] if sig[k] else None
        lines.append(f"{_VCD_VAL.get(first.get('old') if first else None, 'x')}{code}")
    lines.append("$end")
    changes = sorted(((float(e.get("t_ns") or 0), ids[k], _VCD_VAL.get(e.get("new"), "x"))
                      for k, es in sig.items() for e in es), key=lambda x: x[0])
    t_last = None
    for t, code, v in changes:
        ti = int(round(t))
        if ti != t_last:
            lines.append(f"#{ti}")
            t_last = ti
        lines.append(f"{v}{code}")
    return "\n".join(lines) + "\n"


MAX_LINE_CHARS = 2000      # = app.js: a line without a line ending is wrapped here
MAX_LINES = 5000


def uart_lines(evs: list[dict], board: bool, chip0: str) -> dict:
    """The UART transcript from `uart` byte events, split like app.js uartEvent: LF, CR and CRLF end a line (the LF of
    a CRLF adds no empty line), a line is wrapped at MAX_LINE_CHARS, non-printables as \\xNN; RX drops counted."""
    lines: list[dict] = []
    open_: dict[str, dict] = {}
    after_cr: dict[str, bool] = {}
    drops: dict[str, dict] = {}
    for e in evs:
        if e.get("kind") != "uart" or e.get("byte") is None:
            continue
        cid = str(e.get("chip", chip0))
        u = e.get("uart") or e.get("name") or "UART"
        u = f"{cid}.{u}" if board else str(u)
        d = e.get("dir") or "tx"
        if d == "drop":
            x = drops.setdefault(u, {"n": 0, "reason": ""})
            x["n"] += 1
            x["reason"] = e.get("reason") or "?"
            continue
        if d != "tx":
            continue
        b = int(e["byte"])
        if b == 10 and after_cr.get(u):          # the LF of a CRLF: the CR already ended the line
            after_cr[u] = False
            continue
        ln = open_.get(u)
        if ln is None:
            ln = {"uart": u, "chip": cid, "t": e.get("t_ns"), "text": "", "open": True}
            open_[u] = ln
            lines.append(ln)
        if b in (10, 13):
            open_.pop(u, None)
        else:
            ln["text"] += chr(b) if 32 <= b < 127 else f"\\x{b:02x}"
            if len(ln["text"]) >= MAX_LINE_CHARS:
                open_.pop(u, None)
        after_cr[u] = b == 13
    for ln in lines:
        ln["open"] = open_.get(ln["uart"]) is ln
    return {"lines": lines[-MAX_LINES:], "after_cr": after_cr, "drops": drops}


_REL_STIM = re.compile(r"^\s*\+\s*([0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?\s*(?:ns|us|µs|ms|s)?)\s*:(.*)$", re.S)
_ABS_STIM = re.compile(r"^\s*([0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?\s*(?:ns|us|µs|ms|s)?)\s*:(.*)$", re.S)


def apply_stimulus(session: Any, spec: str) -> dict:
    """Session.apply_stimulus with a relative form: "+5ms:PIN=H" = 5 ms after the current virtual time. Runs on the
    worker. Returns {t_ns (absolute; None = now), spec (as applied)}."""
    from ..session import parse_time
    b = getattr(session, "board", None)
    now = b.now_ns() if b is not None else session.chip.clock.now_ns()
    m = _REL_STIM.match(spec)
    t_ns = None
    if m:
        t_ns = now + parse_time(m.group(1))
        spec = f"{t_ns:.3f}ns:{m.group(2)}"
    else:
        m = _ABS_STIM.match(spec)
        if m:
            t_ns = parse_time(m.group(1))
    session.apply_stimulus(spec)
    return {"t_ns": t_ns if t_ns is None or t_ns > now else None, "spec": spec}


def _is_board(session: Any) -> bool:
    return getattr(session, "board", None) is not None


def _chip_ids(session: Any) -> list[str]:
    chips = getattr(session, "chips", None)
    return list(chips) if chips else ["A"]


def _ctx(session: Any, cid: str | None) -> Any:
    """Per-chip context (chip, desc, image) of chip `cid` (case-insensitive); None -> the view chip."""
    ctxs = getattr(session, "ctxs", None)
    if not ctxs:
        if cid is not None and str(cid).strip().upper() not in ("A", ""):
            raise ValueError(f"unknown chip id {cid!r}")
        return session
    if cid is None:
        return ctxs[session.view]
    for k, c in ctxs.items():
        if k.upper() == str(cid).strip().upper():
            return c
    raise ValueError(f"unknown chip id {cid!r} (chips: {', '.join(ctxs)})")


def qualify(session: Any, cid: str, name: str) -> str:
    """'UART0' of chip B -> 'B.UART0' on a board; unchanged for a single-chip session."""
    return f"{cid}.{name}" if _is_board(session) else name


def _uart_blocks(chip: Any, desc: Any) -> list[str]:
    """Peripherals that behave like UARTs: models exposing `tx_listeners` (the board-link hook), plus any
    peripheral whose model name says uart/usart/sci (models without link hooks yet)."""
    out = []
    ub = getattr(chip, "uart_blocks", None)
    if ub is not None:
        out.extend(ub())
    for p in desc.peripherals:
        m = p.model.lower().rsplit(".", 1)[-1]
        if p.name not in out and ("uart" in m or m in ("sci", "usart")):
            out.append(p.name)
    return out


def uart_names(session: Any) -> list[tuple[str, str, str]]:
    """[(chip id, uart name, qualified name)] of every chip."""
    out = []
    for cid in _chip_ids(session):
        c = _ctx(session, cid)
        for u in _uart_blocks(c.chip, c.desc):
            out.append((cid, u, qualify(session, cid, u)))
    return out


def all_pins(session: Any) -> list[dict]:
    """Pin rows of every chip (each with `chip`), plus "configured by <symbol> at <t>" for the tooltips."""
    rows: list[dict] = []
    try:
        from ..pinreport import configured_by
    except Exception:                                   # pragma: no cover
        configured_by = None
    for cid in _chip_ids(session):
        part = session.pin_table(chip=cid) if hasattr(session, "ctxs") else session.pin_table()
        if configured_by is not None:
            try:
                cfg = configured_by(_ctx(session, cid).chip)
            except Exception:
                cfg = {}
            if cfg:
                pre = f"{cid}."
                for row in part:
                    sh = str(row.get("short") or "")
                    c = cfg.get(sh[len(pre):] if sh.startswith(pre) else sh)
                    if c:
                        row.update(c)
        try:
            from ..pinreport import pin_waves
            waves = pin_waves(_ctx(session, cid).chip)
        except Exception:
            waves = {}
        pre = f"{cid}."
        for row in part:
            row.setdefault("chip", cid)
            if waves:
                sh = str(row.get("short") or "")
                w = waves.get(sh[len(pre):] if sh.startswith(pre) else sh)
                if w is not None:            # waveform summary: edge count, frequency and duty of periodic pins
                    row["wave"] = {"edges": w["edges"], "freq_hz": w["freq_hz"], "duty": w["duty"]}
        rows.extend(part)
    return rows


SLIM_KEYS = ("chip", "num", "level", "owner", "direction", "drive", "pull", "external", "net", "cfg_by", "cfg_t",
             "cfg_what")
WAVES_INTERVAL_S = 0.5     # stream: `waves` at most this often
PERIPH_INTERVAL_S = 0.5    # worker: peripheral status (`periph`) recomputed at most this often

_ADC_PATTERNS = (re.compile(r"^ADC\d*_SE\d+[a-z]?$", re.I), re.compile(r"^ADC\d+_IN\d+$", re.I),
                 re.compile(r"^ADCIN[A-Z]\d+$", re.I), re.compile(r"^ADC\d*_D[PM]\d+$", re.I))


def adc_channel(p: Any) -> str | None:
    """The channel name /api/adc (Session.set_adc) takes for pin `p`: the first single-ended ADC input among its
    functions (Kinetis ADC0_SE8, STM32 ADC123_IN0, C2000 ADCINA3), else a differential one; None without analog input."""
    toks: list[str] = []
    for v in list((p.functions or {}).values()) + ([p.name] if p.kind == "analog" else []):
        toks += [t.strip() for t in str(v).split("/")]
    for pat in _ADC_PATTERNS:
        for t in toks:
            if pat.match(t):
                return t
    return None


def slim_row(r: dict) -> dict:
    """The dynamic part of a pin row (what the stream sends; the static part is in /api/chip)."""
    return {k: r.get(k) for k in SLIM_KEYS}


def buses_info(b: Any) -> list[dict]:
    """Non-UART links of board `b` (CAN buses, Ethernet segments, USB host links) with their live counters."""
    if b is None:
        return []
    out: list[dict] = []
    for cl in getattr(b, "can_links", []) or []:
        out.append({"type": "can", "name": cl.name, "nodes": [f"{c}.{p}" for c, p in cl.nodes],
                    "host": cl.host is not None, "describe": cl.describe(), "stats": _bus_stats(cl, "can")})
    for el in getattr(b, "eth_links", []) or []:
        host: Any = False
        if el.host is not None:
            from ..ethernet import ip_str, mac_str
            host = {"ip": ip_str(el.host.ip), "mac": mac_str(el.host.mac)}
        out.append({"type": "eth", "name": el.name, "nodes": [f"{c}.{p}" for c, p in el.nodes], "host": host,
                    "describe": el.describe(), "stats": _bus_stats(el, "eth")})
    for ul in getattr(b, "usb_links", []) or []:
        dev = f"{ul.chip_id}.{ul.port}"
        out.append({"type": "usb", "name": ul.name, "nodes": [dev], "device": dev, "host": True,
                    "describe": ul.describe(), "stats": _bus_stats(ul, "usb")})
    return out


def _bus_stats(ln: Any, typ: str) -> dict:
    try:
        if typ == "can":
            return {"frames": ln.bus.frames, "error_frames": ln.bus.error_frames, "bytes": ln.bus.bytes}
        if typ == "eth":
            d = {"frames": ln.segment.frames, "bytes": ln.segment.bytes}
            if ln.host is not None:
                hs = ln.host.state()
                d["host"] = {k: hs.get(k) for k in ("sent", "received", "ping_replies")}
            return d
        h, bus = ln.host, ln.bus
        dv = h.device
        return {"host_state": h.state, "bus_state": bus.state, "device": dv.describe() if dv is not None else None,
                "transactions": bus.transactions, "bytes_in": bus.bytes_in, "bytes_out": bus.bytes_out}
    except Exception as e:                      # a half-built link must not break the snapshot
        return {"error": f"{type(e).__name__}: {e}"}


def bus_stats(b: Any) -> list[dict]:
    """Live counters of buses_info(b), same order: [{name, type, stats}]."""
    return [{"name": x["name"], "type": x["type"], "stats": x["stats"]} for x in buses_info(b)]


_CAN_IDLE = ("disabled", "sleep", "init", "freeze")


def periph_status(session: Any) -> list[dict]:
    """Status rows of the bus peripherals (CAN controllers, Ethernet MACs, USB device ports on a host link, SD cards):
    [{chip, block, type, ...}]. Call on the worker thread."""
    out: list[dict] = []
    for cid in _chip_ids(session):
        ctx = _ctx(session, cid)
        chip = ctx.chip
        cb = getattr(chip, "can_blocks", None)
        for n, blk in (cb() if cb else {}).items():
            fn = getattr(blk, "can_stats", None)
            if fn is None:
                continue
            try:
                st = fn()
            except Exception:
                continue
            if st.get("state") in _CAN_IDLE and not (st.get("tx") or st.get("rx") or st.get("tec") or st.get("rec")):
                continue
            out.append({"chip": cid, "block": n, "type": "can", **st})
        try:
            from ..ethernet import eth_blocks
            macs = eth_blocks(chip)
        except Exception:
            macs = {}
        for n, blk in macs.items():
            try:
                st = dict(blk.eth_stats())
            except Exception:
                continue
            if not (st.get("tx") or st.get("rx") or st.get("segment") or st.get("link") not in (None, "down")):
                continue
            out.append({"chip": cid, "block": n, "type": "eth", **st})
        for dev in getattr(ctx, "devices", None) or []:
            if hasattr(dev, "capacity") and hasattr(dev, "info") and hasattr(dev, "current_state"):
                try:
                    i = dev.info()
                except Exception:
                    continue
                nm = str(i.get("name") or "SD")
                out.append({"chip": cid, "block": nm[:-5] if nm.endswith("_card") else nm, "type": "sd", "card": nm,
                            **{k: v for k, v in i.items() if k != "name"}})
    for ul in (session.usb_links() if hasattr(session, "usb_links") else []):
        try:
            h, b = ul.host, ul.bus
            dv = h.device
            out.append({"chip": ul.chip_id, "block": ul.port, "type": "usb", "host_state": h.state,
                        "bus_state": b.state, "address": dv.address if dv is not None else None,
                        "device": dv.describe() if dv is not None else None,
                        "strings": ({"manufacturer": dv.manufacturer, "product": dv.product, "serial": dv.serial}
                                    if dv is not None else None),
                        "transactions": b.transactions, "naks": b.naks, "stalls": b.stalls, "timeouts": b.timeouts,
                        "bytes_in": b.bytes_in, "bytes_out": b.bytes_out, "cdc_rx": len(h.cdc_rx),
                        "failures": list(h.failures)[-3:]})
        except Exception:
            continue
    return jsonable(out)


def adc_values(session: Any) -> dict:
    return {cid: dict(getattr(_ctx(session, cid), "adc_values", None) or {}) for cid in _chip_ids(session)}


def _one_chip_info(session: Any, cid: str) -> dict:
    c = _ctx(session, cid)
    chip, d = c.chip, c.desc
    pins = []
    for p in (chip.pins.pins if chip.pins is not None else []):
        pins.append({"num": p.num, "name": p.name, "short": p.id, "qname": qualify(session, cid, p.id),
                     "gpio_name": getattr(p, "gpio_name", None), "kind": p.kind,
                     "functions": dict(p.functions), "port": p.port, "bit": p.bit, "adc": adc_channel(p)})
    pins.sort(key=lambda x: x["num"])
    img = getattr(c, "image", None)
    uarts = _uart_blocks(chip, d)
    return {
        "id": cid, "name": d.name, "package": d.package or f"{len(pins)}-pin", "pin_count": len(pins),
        "clock_hz": chip.clock.hz, "cpu": d.core.cpu, "fpu": d.core.fpu, "family": d.family, "pins": pins,
        "uarts": uarts, "uart_ids": [qualify(session, cid, u) for u in uarts],
        "adcs": [p.name for p in d.peripherals if "adc" in p.model.lower()],
        "firmware": Path(getattr(img, "source", "") or "").name if img is not None else None,
        "firmware_format": getattr(img, "fmt", None) if img is not None else None,
        "load_addr": img.start if img is not None and getattr(img, "segments", None) else None,
    }


def board_info(session: Any) -> dict:
    """The wiring part of /api/chip (and /api/board): {board, nets, links}. Call on the worker thread."""
    b = getattr(session, "board", None)
    if b is None:
        return {"board": None, "nets": [], "links": [], "parts": [], "buses": []}
    return jsonable({
        "board": {"name": b.name, "quantum_ns": b.quantum_ns, "wiring": getattr(b, "wiring", 0)},
        "nets": [_net_dict(n) for n in b.nets],
        "parts": parts_info(b),
        "links": [{"type": "uart", "from": f"{ln.a[0]}.{ln.a[1]}", "to": f"{ln.b[0]}.{ln.b[1]}",
                   "bidir": ln.bidir, "describe": ln.describe(), "auto": getattr(ln, "auto", False),
                   "net": getattr(ln, "net", None),
                   "stats": {"a_to_b": ln.bytes_ab, "b_to_a": ln.bytes_ba}} for ln in b.links],
        "buses": buses_info(b),
    })


def _net_ports(n: Any) -> list[str]:
    """Part endpoints of a net as "PART.port"."""
    out = []
    for ent in getattr(n, "ports", None) or []:
        try:
            part, port = ent
        except (TypeError, ValueError):
            continue
        out.append(f"{getattr(part, 'id', part)}.{port}")
    return out


def _port_dir(d: Any) -> str:
    return str(getattr(d, "dir", d if isinstance(d, str) else "in"))


def parts_info(b: Any) -> list[dict]:
    """[{id, type, params, ports: {port: net name | None}, dirs: {port: dir}, state}] of the board's parts."""
    out = []
    for pid, p in (getattr(b, "parts", None) or {}).items():
        ports: dict[str, Any] = {}
        for port in p.ports:
            try:
                n = b.part_net(pid, port)
            except Exception:
                n = None
            ports[port] = getattr(n, "name", None)
        try:
            st = p.state()
        except Exception as e:                 # a broken part must not hide the others
            st = {"error": f"{type(e).__name__}: {e}"}
        out.append({"id": pid, "type": p.type, "params": dict(getattr(p, "params", {}) or {}), "ports": ports,
                    "dirs": {k: _port_dir(v) for k, v in p.ports.items()}, "state": st})
    return out


def part_states(b: Any) -> dict:
    out = {}
    for pid, p in (getattr(b, "parts", None) or {}).items():
        try:
            out[pid] = p.state()
        except Exception as e:
            out[pid] = {"error": f"{type(e).__name__}: {e}"}
    return out


def part_types() -> list[dict]:
    """The add-part catalog (chipem.parts.registry.catalog()) as a list of {type, params, ports}."""
    from ..parts.registry import catalog
    cat = catalog()
    if isinstance(cat, dict):
        cat = [{"type": k, **(v if isinstance(v, dict) else {})} for k, v in cat.items()]
    out = []
    for c in cat:
        c = dict(c)
        c["ports"] = {k: _port_dir(v) for k, v in (c.get("ports") or {}).items()}
        c.setdefault("params", {})
        out.append(c)
    return jsonable(out)


def _part_id(b: Any, body: dict) -> str:
    pid = str(body.get("id") or "").strip()
    if not pid:
        raise ValueError("missing 'id'")
    for k in getattr(b, "parts", None) or {}:
        if str(k).upper() == pid.upper():
            return k
    raise ValueError(f"unknown part {pid!r}")


def chip_info(session: Any) -> dict:
    """Static description for the UI. The top level keeps the single-chip shape (fields of the view chip);
    `chips` lists every chip; on a board `board`, `nets` and `links` describe the wiring."""
    ids = _chip_ids(session)
    view = getattr(session, "view", ids[0]) or ids[0]
    chips = [_one_chip_info(session, cid) for cid in ids]
    top = next((c for c in chips if c["id"] == view), chips[0])
    info: dict = {**{k: v for k, v in top.items() if k not in ("id", "uart_ids")}, "view": view, "chips": chips,
                  "board": None, "nets": [], "links": [], "parts": [], "buses": []}
    info.update(board_info(session))
    return jsonable(info)


def _board(session: Any) -> Any:
    b = getattr(session, "board", None)
    if b is None:
        raise ValueError("not a board session (start with --board FILE)")
    return b


def _parts_board(session: Any) -> Any:
    """The board to add a part to: a single-chip session becomes a one-chip board (Session.ensure_board)."""
    ens = getattr(session, "ensure_board", None)
    return ens() if ens is not None and getattr(session, "board", None) is None else _board(session)


def _pin_list(b: Any, pins: Any) -> list[tuple[str, str]]:
    from ..board import split_qualified
    if not isinstance(pins, list) or not pins:
        raise ValueError("'pins' must be a non-empty list of qualified pin names, e.g. [\"A.PTC6\", \"B.PA0\"]")
    out = []
    for q in pins:
        cid, rest = split_qualified(str(q), b.ids())
        if cid is None or not rest:
            raise ValueError(f"pin {q!r} must be qualified with a chip id (one of {', '.join(b.ids())})")
        out.append((cid, rest))
    return out


def _endpoint(b: Any, q: Any) -> tuple[str, str]:
    from ..board import split_qualified
    cid, rest = split_qualified(str(q or ""), b.ids())
    if cid is None or not rest:
        raise ValueError(f"link endpoint {q!r} must be CHIP.UART (e.g. A.UART0)")
    return cid, rest


def _net_dict(n: Any) -> dict:
    d = {"name": n.name, "pins": [n.label(i) for i in range(len(n.members))], "pull": n.pull, "drive": n.drive,
         "level": n.level, "conflict": n.conflict}
    ports = _net_ports(n)
    if ports:                                  # part endpoints ("LED1.in"), only on nets that have some
        d["ports"] = ports
    return d


class _Handler(BaseHTTPRequestHandler):
    server_version = "chipem"
    protocol_version = "HTTP/1.1"
    app: "WebApp"     # set on the subclass

    def log_message(self, fmt: str, *args: Any) -> None:     # quiet by default
        if self.app.verbose:
            super().log_message(fmt, *args)

    # ---- responses ---------------------------------------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj: Any, code: int = 200) -> None:
        self._send(code, json.dumps(obj, default=str).encode(), "application/json")

    def _error(self, code: int, msg: str) -> None:
        self._json({"ok": False, "error": msg}, code)

    def _body(self) -> dict:
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            self.close_connection = True         # the chunk data would be read as the next request
            raise ValueError("chunked request bodies are not supported (send Content-Length)")
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        sock = self.connection
        sock.settimeout(BODY_TIMEOUT_S)
        try:
            raw = self.rfile.read(n)
        except TimeoutError:
            self.close_connection = True
            raise ValueError(f"request body incomplete after {BODY_TIMEOUT_S:.0f} s") from None
        finally:
            sock.settimeout(None)
        if len(raw) < n:
            self.close_connection = True
            raise ValueError("request body shorter than Content-Length")
        try:
            v = json.loads(raw or b"{}")
        except json.JSONDecodeError as e:
            raise ValueError(f"bad JSON body: {e}") from None
        if not isinstance(v, dict):
            raise ValueError("JSON body must be an object")
        return v

    # ---- board editor (chipem/boardedit.py) ----------------------------------------------------------------
    def _board_edit(self, path: str, body: dict) -> dict:
        """Structural board changes build a new Session from the edited board description and swap it in (the
        page reloads on the new boot id). Saving writes boards/<name>.yaml."""
        from .. import boardedit as be
        app, w = self.app, self.app.worker
        cur = lambda: w.call(lambda: be.board_dict(app.session))      # noqa: E731
        opt = lambda k: body[k] if k in body else ...                  # noqa: E731
        if path == "/api/board/save":
            d = cur()
            name = str(body.get("name") or d.get("name") or "").strip()
            p = be.save_board(d, name, overwrite=bool(body.get("overwrite")))
            b = app.session.board
            if b is not None:
                w.call(lambda: setattr(b, "name", p.stem))
            return {"saved": str(p), "name": p.stem}
        if path == "/api/board/chip/add":
            d, cid = be.add_chip(cur(), str(body.get("chip") or ""), cid=body.get("id"), firmware=body.get("firmware"),
                                 clock_hz=body.get("clock_hz"), load_addr=body.get("load_addr"))
            return {**app.swap_session(d), "id": cid}
        if path == "/api/board/chip/set":
            d = be.set_chip(cur(), str(body.get("id") or ""), chip=body.get("chip") or None,
                            firmware=opt("firmware"), clock_hz=opt("clock_hz"), load_addr=opt("load_addr"))
            return app.swap_session(d)
        if path == "/api/board/chip/remove":
            return app.swap_session(be.remove_chip(cur(), str(body.get("id") or "")))
        if path == "/api/board/new":
            return app.swap_session(be.new_board(str(body.get("name") or "board"), str(body.get("chip") or ""),
                                                 firmware=body.get("firmware"), clock_hz=body.get("clock_hz")))
        if path == "/api/board/open":
            return app.swap_session(be.open_board(str(body.get("path") or body.get("name") or "")))
        raise ValueError(f"unknown board command {path}")

    # ---- GET ---------------------------------------------------------------------------------------------
    def do_HEAD(self) -> None:
        if urlparse(self.path).path == "/api/stream":     # headers only: never start the endless stream
            return self._send(200, b"", "text/event-stream")
        self.do_GET()

    def do_GET(self) -> None:
        u = urlparse(self.path)
        path = u.path
        qs = parse_qs(u.query)
        app = self.app
        try:
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            if path == "/api/chip":
                return self._json({**app.live_chip_info(), "boot": app.boot})
            if path == "/api/catalog":
                from .. import boardedit
                return self._json(boardedit.catalog())
            if path == "/api/board/setup":            # the board editor's view: chips with chip YAML + firmware paths
                from .. import boardedit
                return self._json(app.worker.call(lambda: boardedit.board_dict(app.session)))
            if path == "/api/state":
                snap = app.worker.snapshot
                return self._json({**snap.get("state", {}), "pins": snap.get("pins", []),
                                   "waves": snap.get("waves", {}), "pins_version": snap.get("pins_version", 0),
                                   "version": snap.get("version", 0), "event_seq": snap.get("event_seq", 0)})
            if path == "/api/hostserial":           # host serial bridges and real-time pacing (snapshot)
                st = app.worker.snapshot.get("state", {})
                return self._json(st.get("host") or {"realtime": {"enabled": False}, "endpoints": []})
            if path == "/api/events":
                since = int(qs.get("since", ["0"])[0] or 0)
                kinds = qs.get("kinds", [None])[0]
                ks = {k for k in kinds.split(",") if k} if kinds else None
                fmt = (qs.get("format", ["json"])[0] or "json").lower()
                limit = int(qs.get("limit", ["0" if fmt != "json" else "2000"])[0] or 0)
                board = _is_board(app.session)
                chip0 = _chip_ids(app.session)[0]
                pred = _and(event_filter(qs.get("chip", [""])[0], qs.get("q", [""])[0], board, chip0),
                            pin_filter(qs.get("pin", [""])[0], board, chip0))
                evs, last = app.worker.events_page(since, ks, limit, pred)
                if fmt in ("txt", "csv"):
                    body = events_text(evs, board, chip0) if fmt == "txt" else events_csv(evs)
                    return self._send(200, body.encode(), ("text/plain" if fmt == "txt" else "text/csv") +
                                      "; charset=utf-8",
                                      {"Content-Disposition": f'attachment; filename="chipem_events.{fmt}"'})
                if fmt != "json":
                    raise ValueError(f"format must be json, txt or csv, not {fmt!r}")
                return self._json({"events": evs, "last": last})
            if path == "/api/uart/lines":       # the transcript of every UART, from the kept uart events
                board = _is_board(app.session)
                chip0 = _chip_ids(app.session)[0]
                with app.worker.cond:
                    evs = [e for e in app.worker.events if e.get("kind") == "uart"]
                    last = app.worker.event_seq
                return self._json({**uart_lines(evs, board, chip0), "last": last})
            if path == "/api/vcd":
                board = _is_board(app.session)
                chip0 = _chip_ids(app.session)[0]
                want = _pin_keys(qs.get("pins", [""])[0], board, chip0) or None
                pred = _and(event_filter(qs.get("chip", [""])[0], "", board, chip0),
                            pin_filter(qs.get("pins", [""])[0], board, chip0))
                evs, _ = app.worker.events_page(0, {"pin"}, 0, pred)
                info = app.chip_info or {}
                name = (info.get("board") or {}).get("name") or info.get("name") or "chipem"
                fn = "".join(c if c.isalnum() or c in "._-" else "_" for c in str(name))
                return self._send(200, events_vcd(evs, str(name), board, chip0, want).encode(),
                                  "text/plain; charset=utf-8",
                                  {"Content-Disposition": f'attachment; filename="{fn}.vcd"'})
            if path == "/api/uart":
                name = (qs.get("uart", ["UART0"])[0] or "UART0").upper()
                since = max(0, int(qs.get("since", ["0"])[0] or 0))
                fmt = (qs.get("format", ["json"])[0] or "json").lower()
                out = bytes(app.worker.call(lambda: app.session.uart_output(name), timeout=10))
                if since > len(out):            # buffer shrank (reset): start over
                    since = 0
                text = out[since:].decode("latin-1")
                if fmt == "txt":
                    fn = "".join(c if c.isalnum() or c in "._-" else "_" for c in name)
                    return self._send(200, text.encode("utf-8"), "text/plain; charset=utf-8",
                                      {"Content-Disposition": f'attachment; filename="{fn}.txt"'})
                if fmt != "json":
                    raise ValueError(f"format must be json or txt, not {fmt!r}")
                return self._json({"uart": name, "text": text, "len": len(out), "since": since})
            if path == "/api/pinreport":
                static = (qs.get("static", ["1"])[0] or "1") not in ("0", "false", "no")
                cid = qs.get("chip", [None])[0] or None

                def _report():                  # runs on the worker: resolve the chip there
                    from ..pinreport import report_dict
                    ctx = _ctx(app.session, cid)
                    d = jsonable(report_dict(ctx.chip, static=static))
                    d["chip"] = getattr(ctx, "id", cid or "A")
                    return d
                return self._json(app.worker.call(_report, timeout=30))
            if path == "/api/stream":
                return self._stream(int(qs.get("since", ["0"])[0] or 0))
            if path == "/api/board":
                def _info():
                    b = _board(app.session)
                    d = board_info(app.session)
                    return {**d["board"], "chips": [{"id": cid, "name": app.session.ctxs[cid].desc.name,
                                                     "uarts": [qualify(app.session, cid, u) for u in
                                                               _uart_blocks(c, app.session.ctxs[cid].desc)]}
                                                    for cid, c in b.chips.items()],
                            "nets": d["nets"], "links": d["links"], "parts": d.get("parts", [])}
                return self._json(app.worker.call(_info))
            if path == "/api/parts/types":
                return self._json(part_types())
            if path == "/api/board/yaml":
                text = app.worker.call(lambda: _board(app.session).to_yaml())
                name = app.worker.call(lambda: _board(app.session).name)
                extra = {"Content-Disposition": f'attachment; filename="{name}.yaml"'} \
                    if (qs.get("download", ["0"])[0] or "0") not in ("0", "false", "no") else None
                return self._send(200, text.encode(), "text/yaml; charset=utf-8", extra)
            self._error(404, f"not found: {path}")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except ValueError as e:
            self._error(400, str(e))
        except FutureTimeout:
            self._error(503, "emulator busy (timeout)")
        except Exception as e:
            self._error(500, f"{type(e).__name__}: {e}")

    def _static(self, rel: str) -> None:
        base = STATIC_DIR.resolve()
        p = (base / rel).resolve()
        if base not in p.parents and p != base or not p.is_file():
            return self._error(404, f"not found: {rel}")
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        if p.suffix == ".js":
            ctype = "text/javascript"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self._send(200, p.read_bytes(), ctype)

    def _stream(self, since: int) -> None:
        """Server-Sent Events: `state` snapshots (≤ every 50 ms while something changes, else a 1 s heartbeat)
        and `event` items for each new log event (mmio excluded unless ?mmio=1)."""
        app = self.app
        w = app.worker
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        include_mmio = "mmio=1" in (urlparse(self.path).query or "")
        last_version = -1
        last_pins = -1
        last_epoch = None
        sent_slim: dict = {}                          # (chip, num) -> slim row as last sent on this stream
        last_waves = -1
        waves_t = 0.0
        last_seq = since if since else w.event_seq    # by default only new events; history via /api/events
        last_sent = 0.0
        out = self.wfile

        def send(kind: str, data: Any, ident: int | None = None) -> None:
            msg = f"event: {kind}\n" + (f"id: {ident}\n" if ident is not None else "") + \
                  f"data: {json.dumps(data, default=str, separators=(',', ':'))}\n\n"
            out.write(msg.encode())

        out.write(b"retry: 1000\n\n")
        out.flush()
        while app.serving and w.alive:
            with w.cond:
                if w.version == last_version and w.event_seq == last_seq:
                    w.cond.wait(timeout=1.0)
                version, evseq, snap = w.version, w.event_seq, w.snapshot
            if evseq != last_seq:
                evs, last_seq = w.events_page(last_seq, None, 500)                  # rest on the next pass
                for ev in evs:
                    if ev.get("kind") == "mmio" and not include_mmio:
                        continue
                    send("event", ev, ev.get("seq"))
            now = time.monotonic()
            due = (version != last_version and now - last_sent >= 0.05) or now - last_sent >= 1.0
            if due:
                # pins: the dynamic fields only (static ones are in /api/chip); the full set on the first frame and
                # after a reset / session swap (`pins_full`), else only the rows that changed since this stream's
                # previous frame. `waves` (edge counts change all the time) at most every WAVES_INTERVAL_S.
                pv = snap.get("pins_version", 0)
                frame = {**snap.get("state", {}), "version": version, "pins_version": pv,
                         "event_seq": snap.get("event_seq", 0), "boot": app.boot}
                if pv != last_pins or snap.get("pins_epoch") != last_epoch:
                    cur = snap.get("slim", {})
                    if snap.get("pins_epoch") != last_epoch or not sent_slim:
                        frame["pins"] = list(cur.values())
                        frame["pins_full"] = True
                    else:
                        frame["pins"] = [r for k, r in cur.items() if sent_slim.get(k) != r]
                        frame["pins_full"] = False
                    sent_slim = cur
                    last_pins = pv
                    last_epoch = snap.get("pins_epoch")
                    if not frame["pins"] and not frame["pins_full"]:
                        del frame["pins"], frame["pins_full"]
                wv = snap.get("waves_version", 0)
                if wv != last_waves and (now - waves_t >= WAVES_INTERVAL_S or last_waves < 0):
                    frame["waves"] = snap.get("waves", {})
                    last_waves = wv
                    waves_t = now
                send("state", frame)
                last_version = version
                last_sent = now
            out.flush()
            if not due:
                time.sleep(max(0.0, min(0.05, 0.05 - (now - last_sent))) or 0.01)

    # ---- POST --------------------------------------------------------------------------------------------
    def do_POST(self) -> None:
        path = urlparse(self.path).path
        app = self.app
        w = app.worker
        s = app.session
        try:
            body = self._body()
            if path == "/api/run":
                ms = body.get("ms")
                if ms is not None:
                    ms = float(ms)
                    if not math.isfinite(ms) or ms <= 0:
                        raise ValueError("ms must be a finite number > 0")
                return self._json({"ok": True, **w.cmd_run(ms)})
            if path == "/api/pause":
                w.pause()
                w.call(lambda: setattr(w, "last_stop", "PAUSED"))
                return self._json({"ok": True})
            if path == "/api/step":
                chip = body.get("chip")
                return self._json({"ok": True, **w.cmd_step(int(body.get("n", 1)), str(chip) if chip else None)})
            if path == "/api/restart":                # same setup, virtual time 0 (the page reloads on the new boot)
                return self._json({"ok": True, **app.restart()})
            if path == "/api/stimulus":
                spec = str(body.get("spec") or "").strip()
                if not spec:
                    raise ValueError("missing 'spec' (e.g. 10ms:PTC6=0, +5ms:B.PA0=H, PTC6=H, 1ms:UART0=hi\\r\\n)")
                return self._json({"ok": True, **w.call(lambda: apply_stimulus(w.s, spec))})
            if path == "/api/reset":
                w.pause()                            # also ends a step loop the reset would wait behind
                return self._json({"ok": True, **w.cmd_reset()})
            if path == "/api/pin":
                from ..cmd_shell import parse_level
                name = str(body.get("name") or body.get("pin") or "")
                if not name:
                    raise ValueError("missing 'name'")
                lvl = body.get("level")
                lvl = None if lvl is None else parse_level(lvl)

                def fn():                       # qualified names ("B.PTC6") address any board chip
                    s.set_pin(name, lvl)
                    if hasattr(s, "resolve"):
                        ctx, pin = s.resolve(name)
                        cid = getattr(ctx, "id", "A")
                    else:
                        ctx, pin, cid = s, name, "A"
                    p = ctx.chip.pins.get(pin)
                    return {"name": qualify(s, cid, p.id), "chip": cid, "external": p.external,
                            "level": p.level()}
                return self._json({"ok": True, **w.call(fn)})
            if path == "/api/uart":
                from ..cmd_shell import unescape
                uart = str(body.get("uart") or "UART0").upper()
                text = body.get("text", "")
                data = unescape(text) if body.get("escapes", True) else str(text).encode()
                # the host starts sending now, paced at the UART's baud; bytes the receiver is not ready for are
                # dropped (uart events dir "drop", counted in the console)
                w.call(lambda: s.uart_input(data, uart))
                return self._json({"ok": True, "uart": uart, "bytes": len(data), "paced": True})
            if path == "/api/adc":
                ch = str(body.get("channel") or "")
                if not ch:
                    raise ValueError("missing 'channel'")
                raw = body.get("value")             # JSON int = raw code, JSON float = volts, string: "1.65V", "1200mV"
                if isinstance(raw, bool) or raw is None:
                    raise ValueError("missing or bad 'value'")
                if isinstance(raw, (int, float)):
                    val = raw
                else:
                    from ..session import parse_adc_value
                    val = parse_adc_value(str(raw))
                w.call(lambda: s.set_adc(ch, val))
                return self._json({"ok": True, "channel": ch, "value": val})
            if path == "/api/realtime":             # {on: bool, speed?: float} real-time pacing (Session.set_realtime)
                on = body.get("on", True)
                on = on if isinstance(on, bool) else str(on).strip().lower() in ("1", "true", "yes", "on")
                speed = body.get("speed")
                speed = None if speed in (None, "") else float(speed)
                if speed is not None and (not math.isfinite(speed) or speed <= 0):
                    raise ValueError("speed must be a finite number > 0")

                def fn():
                    w.s.set_realtime(on, speed)
                    w.realtime = on
                    w._rt_anchor = None
                    return w.s.host_status()["realtime"]
                return self._json({"ok": True, "realtime": w.call(fn)})
            if path in ("/api/net", "/api/net/rename", "/api/net/remove", "/api/link", "/api/link/remove",
                        "/api/part", "/api/part/set", "/api/part/remove", "/api/part/port"):
                return self._json({"ok": True, **w.call(lambda: self._wiring(path, body))})
            if path.startswith("/api/board/"):
                return self._json({"ok": True, **self._board_edit(path, body)})
            self._error(404, f"not found: {path}")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except FutureTimeout:
            self._error(503, "emulator busy (timeout)")
        except FileExistsError as e:
            self._error(409, str(e))
        except (ValueError, KeyError, TypeError, FileNotFoundError) as e:
            self._error(400, str(e.args[0] if e.args else e))
        except Exception as e:
            self._error(500, f"{type(e).__name__}: {e}")

    def do_DELETE(self) -> None:
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        w = self.app.worker
        try:
            if u.path.startswith("/api/net/"):
                from urllib.parse import unquote
                name = unquote(u.path[len("/api/net/"):])
                return self._json({"ok": True, **w.call(lambda: self._wiring("/api/net/remove", {"name": name}))})
            if u.path == "/api/link":
                body = {k: v[0] for k, v in qs.items()}
                if "index" in body:
                    body["index"] = int(body["index"])
                return self._json({"ok": True, **w.call(lambda: self._wiring("/api/link/remove", body))})
            self._error(404, f"not found: {u.path}")
        except FutureTimeout:
            self._error(503, "emulator busy (timeout)")
        except (ValueError, KeyError, TypeError) as e:
            self._error(400, str(e.args[0] if e.args else e))
        except Exception as e:
            self._error(500, f"{type(e).__name__}: {e}")

    def _wiring(self, path: str, body: dict) -> dict:
        """Runs on the worker: the board wiring commands."""
        b = _parts_board(self.app.session) if path == "/api/part" else _board(self.app.session)
        if path == "/api/net":
            pull = body.get("pull") or None
            name = body.get("name") or None
            net = b.connect(_pin_list(b, body.get("pins")), name=str(name) if name else None,
                            pull=str(pull).lower() if pull else None)
            return {"net": _net_dict(net), "wiring": b.wiring}
        if path == "/api/net/rename":
            net = b.rename_net(str(body.get("name") or ""), str(body.get("new") or ""))
            return {"net": _net_dict(net), "wiring": b.wiring}
        if path == "/api/net/remove":
            name = str(body.get("name") or "")
            if not name:
                raise ValueError("missing 'name'")
            b.remove_net(name)
            return {"removed": name, "wiring": b.wiring}
        if path == "/api/link":
            ln = b.add_link(_endpoint(b, body.get("from")), _endpoint(b, body.get("to")),
                            bidir=bool(body.get("bidir", True)))
            return {"link": ln.to_dict(), "wiring": b.wiring}
        if path == "/api/link/remove":
            if body.get("index") is not None:
                i = int(body["index"])
                if not 0 <= i < len(b.links):
                    raise ValueError(f"link index {i} out of range (0..{len(b.links) - 1})")
                ln = b.links[i]
            else:
                ln = b.find_link(_endpoint(b, body.get("from")), _endpoint(b, body.get("to")))
            desc = ln.describe()
            b.remove_link(ln)
            return {"removed": desc, "wiring": b.wiring}
        if path == "/api/part":
            typ = str(body.get("type") or "").strip()
            if not typ:
                raise ValueError("missing 'type'")
            known = {t["type"] for t in part_types()}
            if typ not in known:
                raise ValueError(f"unknown part type {typ!r} (types: {', '.join(sorted(known))})")
            params = body.get("params") or {}
            ports = body.get("ports") or {}
            if not isinstance(params, dict) or not isinstance(ports, dict):
                raise ValueError("'params' and 'ports' must be objects")
            pid = str(body.get("id") or "").strip() or None       # None: the board picks SW1, LED2, U3, ...
            if pid is not None and any(str(k).upper() == pid.upper() for k in b.parts):
                raise ValueError(f"part {pid!r} already exists")
            ports = {str(k): (str(v) if v not in (None, "") else None) for k, v in ports.items()}
            p = b.add_part(pid, typ, dict(params), {k: v for k, v in ports.items() if v})
            info = next((x for x in parts_info(b) if x["id"] == p.id), None)
            return {"part": info, "wiring": b.wiring}
        if path == "/api/part/set":
            pid = _part_id(b, body)
            values = body.get("values")
            if not isinstance(values, dict) or not values:
                raise ValueError("'values' must be a non-empty object, e.g. {\"level\": \"H\"}")
            st = b.set_part(pid, **values)
            return {"id": pid, "state": st, "wiring": b.wiring}
        if path == "/api/part/remove":
            pid = _part_id(b, body)
            b.remove_part(pid)
            return {"removed": pid, "wiring": b.wiring}
        if path == "/api/part/port":
            pid = _part_id(b, body)
            port = str(body.get("port") or "")
            if port not in b.parts[pid].ports:
                raise ValueError(f"part {pid} has no port {port!r} (ports: {', '.join(b.parts[pid].ports)})")
            to = body.get("to")
            if to in (None, ""):
                b.disconnect_port(pid, port)
            else:
                b.connect_port(pid, port, str(to))
            n = b.part_net(pid, port)
            return {"id": pid, "port": port, "net": getattr(n, "name", None), "wiring": b.wiring}
        raise ValueError(f"unknown wiring command {path}")


class WebApp:
    """Session + Worker + HTTP server.  `start()` runs everything in background threads; `stop()` tears down."""

    def __init__(self, session: Any, host: str = "127.0.0.1", port: int = 8765, *, realtime: bool = False,
                 chunk_ms: float = 5.0, verbose: bool = False, factory: Callable[[], Any] | None = None):
        self.session = session
        # restart (POST /api/restart): `factory` rebuilds the session as it was started (CLI options included) while
        # it is still that session with its original wiring; afterwards the current setup is rebuilt (boardedit)
        self.factory = factory
        self._factory_wiring = (session, getattr(getattr(session, "board", None), "wiring", None))
        self.verbose = verbose
        self.boot = uuid.uuid4().hex[:12]        # changes on every server start: an open page reloads on a new one
        self.chip_info = chip_info(session)
        self.worker = Worker(session, chunk_ns=chunk_ms * 1e6, realtime=realtime)
        handler = type("Handler", (_Handler,), {"app": self})
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.httpd.daemon_threads = True
        self.serving = False
        self._http_thread: threading.Thread | None = None

    def live_chip_info(self) -> dict:
        """chip_info with the current wiring and the board's live net levels and link byte counts from the latest
        snapshot."""
        info = self.worker.full_info or self.chip_info
        b = (self.worker.snapshot.get("state") or {}).get("board")
        if not b or not info.get("board"):
            return info
        info = {**info, **(self.worker.wiring_info or {})}
        lv = {n.get("name"): n for n in b.get("nets", [])}
        info["nets"] = [{**n, "level": lv.get(n["name"], {}).get("level", n.get("level")),
                         "conflict": lv.get(n["name"], {}).get("conflict", False)} for n in info["nets"]]
        links = b.get("links", [])
        info["links"] = [{**ln, "stats": {"a_to_b": links[i].get("a_to_b", 0), "b_to_a": links[i].get("b_to_a", 0)}}
                         if i < len(links) else ln for i, ln in enumerate(info["links"])]
        live = {(x.get("type"), x.get("name")): x.get("stats") for x in b.get("buses", [])}
        info["buses"] = [{**x, "stats": live.get((x.get("type"), x.get("name")), x.get("stats"))}
                         for x in info.get("buses", [])]
        return info

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    @property
    def url(self) -> str:
        host = self.httpd.server_address[0]
        return f"http://{host}:{self.port}/"

    def restart(self) -> dict:
        """Rebuild the current setup with virtual time 0 (POST /api/restart): same chips, firmware, wiring and parts.
        Host serial ports of the old session are closed first (a TCP port or PTY link can be reopened)."""
        from .. import boardedit
        w = self.worker
        w.pause()
        old = self.session
        orig, wiring = self._factory_wiring
        use_factory = self.factory is not None and old is orig and \
            getattr(getattr(old, "board", None), "wiring", None) == wiring
        d = None if use_factory else w.call(lambda: boardedit.board_dict(old))
        view = getattr(old, "view", None)
        rt = getattr(old, "pacer", None)
        speed = getattr(rt, "speed", None) if rt is not None else None
        w.call(lambda: old.close_hosts() if hasattr(old, "close_hosts") else None)
        new = self.factory() if use_factory else boardedit.build_session(d, like=old)
        if view is not None and getattr(new, "board", None) is not None and hasattr(new, "set_view"):
            try:
                new.set_view(view)
            except Exception:
                pass
        if speed is not None and hasattr(new, "set_realtime") and getattr(new, "pacer", None) is None:
            new.set_realtime(True, speed)

        def on_swap():
            self.session = new
            self.chip_info = chip_info(new)
            self.boot = uuid.uuid4().hex[:12]
            if use_factory:
                self._factory_wiring = (new, getattr(getattr(new, "board", None), "wiring", None))
        w.replace_session(new, on_swap)
        w.realtime = getattr(new, "pacer", None) is not None
        try:
            old.close()
        except Exception:
            pass
        return {"boot": self.boot, "chips": _chip_ids(new)}

    def swap_session(self, d: dict) -> dict:
        """Build a Session from board mapping `d` (chipem.boardedit) and run it instead of the current one. On a
        build error the current session keeps running untouched."""
        from .. import boardedit
        new = boardedit.build_session(d, like=self.session)

        def on_swap():                          # on the worker thread, before the first snapshot of `new`
            self.session = new
            self.chip_info = chip_info(new)
            self.boot = uuid.uuid4().hex[:12]
        old = self.worker.replace_session(new, on_swap)
        try:
            old.close()
        except Exception:
            pass
        return {"board": d.get("name"), "chips": [c["id"] for c in d.get("chips") or []], "boot": self.boot}

    def _warm_catalog(self) -> None:
        try:
            from .. import boardedit
            boardedit.catalog()                 # loads every chip YAML once (~2 s); the Board dialog is then instant
        except Exception:
            pass

    def start(self) -> "WebApp":
        threading.Thread(target=self._warm_catalog, name="chipem-catalog", daemon=True).start()
        self.serving = True
        self.worker.start()
        self._http_thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.2},
                                             name="chipem-http", daemon=True)
        self._http_thread.start()
        return self

    def serve_forever(self) -> None:
        """Blocking variant (main thread runs HTTP); Ctrl-C returns."""
        threading.Thread(target=self._warm_catalog, name="chipem-catalog", daemon=True).start()
        self.serving = True
        self.worker.start()
        try:
            self.httpd.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self) -> None:
        self.serving = False
        self.worker.shutdown()
        if self._http_thread is not None:
            self.httpd.shutdown()
            self._http_thread.join(5)
        self.httpd.server_close()
