"""`chipem run`: run firmware for a virtual time span, print events live, then a summary.

Exit status: 0 = ran to the end of the window (or was interrupted), 3 = stopped on a fault / lockup.
"""
from __future__ import annotations

import sys
import time

from .core import StopReason
from .log import KINDS
from .session import Session, parse_time
from .trace import PinEventPrinter, escape_bytes

EVENT_KINDS = tuple(KINDS)


def add_args(p) -> None:
    p.add_argument("--for", dest="duration", default=None, metavar="TIME",
                   help="virtual time to run, e.g. 20ms, 1.5s, 500us (default 100ms; bare number = ms; with a host "
                        "serial port attached: until Ctrl-C; 'forever' = until Ctrl-C)")
    p.add_argument("--quiet", "-q", action="store_true", help="no live event output (summary only)")
    p.add_argument("--summary-only", action="store_true", help="same as --quiet")
    p.add_argument("--events", default=None, metavar="KINDS",
                   help=f"comma list of event kinds to print live ({','.join(EVENT_KINDS)}; default: all but mmio, "
                        "mmio included with --log-mmio)")
    p.add_argument("--all-edges", action="store_true",
                   help="print every pin and board-part edge; by default a signal with 3+ edges within 10 ms "
                        "(PWM, clocks and what they drive) prints one waveform summary per 10 ms window")
    p.add_argument("--stop-on-fault", action="store_true",
                   help="stop at the first fault instead of entering the firmware's HardFault handler")


def _fmt_hz(hz: float) -> str:
    if hz >= 1e6:
        return f"{hz / 1e6:g} MHz"
    if hz >= 1e3:
        return f"{hz / 1e3:g} kHz"
    return f"{hz:g} Hz"


def _fmt_ns(ns: float) -> str:
    for scale, unit in ((1e9, "s"), (1e6, "ms"), (1e3, "us")):
        if ns >= scale:
            return f"{ns / scale:.3f}".rstrip("0").rstrip(".") + f" {unit}"
    return f"{ns:.0f} ns"


def changed_pins(session: Session, chip: str | None = None) -> list[dict]:
    """GPIO-capable pins whose owner/direction/pull differs from the state right after reset."""
    ctx = session._ctx(chip)
    board = ctx.chip.pins
    if board is None:
        return []
    initial = ctx.reset_pins or {n: (o, d, pl) for n, (o, d, _dr, pl) in getattr(board, "_initial", {}).items()}
    out = []
    for row in session.pin_table(chip=ctx.id):
        if row["kind"] != "gpio":
            continue
        init = initial.get(row["num"])
        if init is None:
            continue
        owner, direction, pull = init
        if (row["owner"], row["direction"], row["pull"]) != (owner, direction, pull):
            out.append(row)
    return out


def _prefixed(text: str, prefix: str) -> str:
    if not prefix:
        return text
    return "".join((prefix + ln if ln.strip() else ln) for ln in text.splitlines(keepends=True))


def print_summary(session: Session, *, reason: StopReason | None, wall_s: float, t0_ns: float, c0: int,
                  out=None, i0: int | None = None, chip: str | None = None, stop_note: str = "") -> None:
    out = out or sys.stdout
    w = out.write
    ctx = session._ctx(chip)
    cid = ctx.id
    chip = ctx.chip
    dt_ns = chip.clock.now_ns() - t0_ns
    cycles = chip.clock.cycles - c0
    insns = getattr(chip, "insns", 0) - i0 if i0 is not None else cycles
    multi = len(session.chips) > 1
    w(f"\n=== summary{f' chip {cid} ({ctx.desc.name})' if multi else ''} ===\n")
    st = session.state(chip=cid)
    w(f"virtual time : {_fmt_ns(dt_ns)} (now {_fmt_ns(st['t_ns'])}), core clock {_fmt_hz(chip.clock.hz)}\n")
    if multi:
        w(f"firmware     : {getattr(ctx.image, 'source', '-')}\n")
    speed = insns / wall_s if wall_s > 0 else 0.0
    ratio = (dt_ns / 1e9) / wall_s if wall_s > 0 else 0.0
    if getattr(chip.core, "cycle_counted", False):            # MCS-51: clock periods, machine cycles of 12 or 6
        mc = getattr(chip.core, "clocks_per_mc", 12)
        w(f"cycles       : {cycles:,} oscillator periods ({cycles // mc:,} machine cycles of {mc} clocks), "
          f"incl. time slept\n")
        w(f"instructions : {insns:,} executed (1, 2 or 4 machine cycles each)\n")
    else:
        w(f"cycles       : {cycles:,} (virtual core cycles, incl. time slept in WFI)\n")
        w(f"instructions : {insns:,} executed (1 cycle/insn model)\n")
    share = ""
    if multi and hasattr(session, "speed_info"):
        cs = (session.speed_info().get("chips") or {}).get(cid) or {}
        if cs.get("share") is not None:
            share = f", {cs['share'] * 100:.0f} % of the board's wall time"
    w(f"wall time    : {wall_s:.3f} s, {speed / 1e6:.2f} M insn/s, {ratio:.3g}x real time"
      f"{' (board wall time)' if multi else ''}{share}\n")
    w(f"stop         : {reason.name if reason is not None else 'interrupted'}{stop_note}; pc=0x{st['pc']:08X} "
      f"mode={st['mode']}{' (' + st['active_name'] + ')' if st.get('active_name') else ''}"
      f"{' sleeping' if st.get('sleeping') else ''}{' LOCKUP' if st.get('lockup') else ''}\n")
    if chip.last_fault:
        f = chip.last_fault
        w(f"last fault   : {f.get('type')} pc=0x{(f.get('pc') or 0):08X}"
          + (f" addr=0x{f['addr']:08X}" if f.get("addr") is not None else "") + "\n")

    rows = changed_pins(session, cid)
    w(f"\npins configured by firmware ({len(rows)}):\n")
    if rows:
        w(f"  {'pin':<6} {'num':>3}  {'owner':<14} {'dir':<6} {'lvl':<3} {'pull':<5} ext\n")
        for r in rows:
            short = r["short"]
            if short.upper().startswith(f"{cid}.".upper()):          # qualified "B.PTC5" (not the "P1.0" of an 80C51)
                short = short[len(cid) + 1:]
            w(f"  {short:<6} {r['num']:>3}  {str(r['owner']):<14} {r['direction']:<6} {r['level']:<3} "
              f"{str(r['pull'] or '-'):<5} {r['external'] or '-'}" + (f"  net {r['net']}" if r.get("net") else "")
              + "\n")
    else:
        w("  (none)\n")

    w("\nUART output:\n")
    any_uart = False
    for name, blk in chip.uart_blocks().items():
        data = getattr(blk, "output", None)
        if data:
            any_uart = True
            text = escape_bytes(bytes(data))
            if len(text) > 2000:
                text = text[:2000] + f"... ({len(data)} bytes)"
            w(f"  {name} ({len(data)} bytes): \"{text}\"\n")
    drops: dict[str, dict[str, int]] = {}
    errs: dict[str, dict[str, int]] = {}
    for ev in session.events(kinds={"uart"}, chip=cid if multi else None):
        if ev.fields.get("dir") == "drop":
            d = drops.setdefault(str(ev.fields.get("uart")), {})
            why = str(ev.fields.get("reason"))
            d[why] = d.get(why, 0) + 1
        elif ev.fields.get("dir") == "rx":
            for k, label in (("fe", "framing"), ("pe", "parity"), ("nf", "noise"), ("brk", "break")):
                if ev.fields.get(k):
                    e = errs.setdefault(str(ev.fields.get("uart")), {})
                    e[label] = e.get(label, 0) + 1
    for name, e in errs.items():
        any_uart = True
        w(f"  {name} RX errors: " + ", ".join(f"{k} {v}" for k, v in e.items())
          + " (the sender's frame format or baud differs from the receiver's, or a break)\n")
    for name, d in drops.items():
        any_uart = True
        w(f"  {name} RX dropped {sum(d.values())} byte(s): "
          + ", ".join(f"{k} {v}" for k, v in d.items())
          + " (the receiver was not listening or overran; time input with --at TIME:UART=TEXT)\n")
    if not any_uart:
        w("  (none)\n")

    _print_can(session, chip, cid if multi else None, w)
    _print_eth(chip, w)
    _print_usb(session, chip, cid, w)

    irqs = session.irq_summary(cid)
    w("\nexceptions taken:\n")
    if irqs:
        exc_label = getattr(chip.arch, "exc_label", None)      # arch-specific numbering (C28x: "PIE 1.7")
        for exc, name, n in irqs:
            if exc_label is not None:
                label = f"{exc_label(exc)} {name}"
            else:
                label = f"IRQ{exc - 16} {name}" if exc >= 16 else f"exc {exc} {name}"
            w(f"  {label:<24} {n:>8}\n")
    else:
        w("  (none)\n")

    _print_warnings(session.events(kinds={"warn", "fault"}, chip=cid if multi else None), w)


def _print_usb(session: Session, chip, cid, w) -> None:
    """USB ports plugged into an emulated host (chipem/usbbus.py): enumerated device, CDC-ACM output."""
    links = [ln for ln in session.usb_links() if ln.block.chip is chip] if hasattr(session, "usb_links") else []
    if not links:
        return
    w("\nUSB:\n")
    for ln in links:
        h, b = ln.host, ln.bus
        devs = h.devices
        w(f"  {ln.port}: host {h.state}, bus {b.state}; {b.transactions} transactions ({b.naks} NAK, {b.stalls} "
          f"STALL, {b.timeouts} timeouts), {b.sofs} SOFs, {b.bytes_in} bytes in / {b.bytes_out} out\n")
        for d in devs:
            w(f"    enumerated: {d.describe()}\n")
        for f in h.failures:
            w(f"    failed: {f}\n")
        if not devs and not h.failures:
            w("    no device enumerated" + (" (no pull-up seen: is the port connected / VBUS sensed?)"
                                           if b.state == "detached" else "") + "\n")
        if h.cdc_rx:
            text = escape_bytes(bytes(h.cdc_rx))
            if len(text) > 2000:
                text = text[:2000] + f"... ({len(h.cdc_rx)} bytes)"
            w(f"    CDC-ACM output ({len(h.cdc_rx)} bytes): \"{text}\"\n")
        hid = h.hid
        if hid is not None and hid.reports:
            w(f"    HID reports: {len(hid.reports)}, last {hid.reports[-1][1].hex()}\n")


def _print_eth(chip, w) -> None:
    """Ethernet MACs that did something (chipem.ethernet.eth_blocks, eth_stats())."""
    from .ethernet import eth_blocks
    for n, b in eth_blocks(chip).items():
        st = b.eth_stats()
        if not (st["tx"] or st["rx"] or st["filtered"] or st["missed"] or st["segment"]):
            continue
        w(f"\nEthernet {n}: {st['iface'].upper()}, PHY {st['phy']}@{st['phy_addr']} link {st['link']}, "
          f"{st['tx']} tx, {st['rx']} rx" + "".join(f", {st[k]} {k}" for k in ("filtered", "crc", "missed", "lost")
                                                      if st[k]) + (f", segment {st['segment']}" if st["segment"] else "")
          + "\n")


def _print_can(session: Session, chip, cid, w, limit: int = 20) -> None:
    """CAN controllers that did something (chip.can_blocks(), can_stats()) and the frames this chip sent."""
    blocks = chip.can_blocks() if hasattr(chip, "can_blocks") else {}
    active = {n: b.can_stats() for n, b in blocks.items() if hasattr(b, "can_stats")}
    active = {n: st for n, st in active.items() if st["tx"] or st["rx"] or st["tec"] or st["rec"]
              or st["state"] not in ("disabled", "init", "freeze", "sleep", "active")}
    evs = [ev for ev in session.events(kinds={"can"}, chip=cid)]
    if not active and not evs:
        return
    from .log import format_can
    ok = [ev for ev in evs if not ev.fields.get("err")]
    w(f"\nCAN frames: {len(ok)} sent" + (f", {len(evs) - len(ok)} failed attempt(s) logged" if len(ok) != len(evs)
                                          else "") + "\n")
    for n, st in active.items():
        rate = f"{st['bitrate'] / 1e3:g} kbit/s" if st["bitrate"] else "no bit clock"
        w(f"  {n}: {rate}, {st['tx']} tx, {st['rx']} rx" + (f", {st['lost']} not stored" if st["lost"] else "")
          + f", TEC {st['tec']} REC {st['rec']}, {st['state']}" + (f", bus {st['bus']}" if st.get("bus") else "")
          + "\n")
    for ev in evs[:limit]:
        w(f"  {ev.t_ns / 1e6:10.3f} ms  {format_can(ev.fields)}\n")
    if len(evs) > limit:
        w(f"  ... {len(evs) - limit} more\n")


def _print_warnings(events, w) -> None:
    warns: dict[str, int] = {}
    missing: list[str] = []
    for ev in events:
        m = str(ev.fields.get("msg", ev.fields))
        if ev.kind == "warn" and ev.fields.get("model") and "not available" in m:
            missing.append(f"{ev.fields.get('block')}({ev.fields['model']})")
            continue
        warns[m] = warns.get(m, 0) + 1
    if missing:
        w(f"\nunmodelled peripherals (RAM-like stubs): {', '.join(missing)}\n")
    if warns:
        w(f"\nwarnings/faults ({sum(warns.values())}):\n")
        for m, n in list(warns.items())[:40]:
            w(f"  {m}" + (f"  (x{n})" if n > 1 else "") + "\n")
        if len(warns) > 40:
            w(f"  ... {len(warns) - 40} more distinct messages\n")


def print_board_summary(session: Session, out=None) -> None:
    """Nets, links and board-level warnings of a multi-chip session."""
    out = out or sys.stdout
    w = out.write
    b = session.board
    if b is None:
        return
    w(f"\n=== board {b.name}: {len(b.chips)} chips ({', '.join(b.chips)}), quantum {_fmt_ns(b.quantum_ns)}, "
      f"time skew {_fmt_ns(b.skew_ns())} ===\n")
    if hasattr(session, "speed_info"):
        from .cmd_shell import format_speed
        p = session.perf
        w(f"{format_speed(session)}\n")
        w(f"settings: preset {p.preset}, max slice {p.max_slice}, quantum {_fmt_ns(b.quantum_ns)}"
          f"{' + auto' if b.auto_quantum else ''} (cross-chip latency up to one quantum)\n")
    w(f"nets ({len(b.nets)}):\n")
    for n in b.nets:
        pins = ", ".join(n.label(i) for i in range(len(n.members)))
        extra = (f" pull={n.pull}" if n.pull else "") + (f" drive={n.drive}" if n.drive else "")
        extra += f" conflicts={n.conflicts}" if n.conflicts else ""
        if n.ports:
            extra += " parts " + ", ".join(f"{p.id}.{port}" for p, port in n.ports)
        w(f"  {n.name:<12} {n.level}  [{pins}]{extra}\n")
    if not b.nets:
        w("  (none)\n")
    w(f"links ({len(b.links) + len(getattr(b, 'can_links', []))}):\n")
    for ln in b.links:
        w(f"  uart {ln.describe()}: {ln.bytes_ab} bytes {ln.a[0]}->{ln.b[0]}"
          + (f", {ln.bytes_ba} bytes {ln.b[0]}->{ln.a[0]}" if ln.bidir else "") + "\n")
    for cl in getattr(b, "can_links", []):
        w(f"  can {cl.describe()}: {cl.bus.frames} frames, {cl.bus.error_frames} error frames\n")
    for el in getattr(b, "eth_links", []):
        h = el.host
        w(f"  eth {el.describe()}: {el.segment.frames} frames" + (f", host ping replies {len(h.replies)}" if h else "")
          + "\n")
    for ul in getattr(b, "usb_links", []):
        d = ul.host.device
        w(f"  usb {ul.describe()}: {ul.host.state}" + (f", {d.vid:04X}:{d.pid:04X}" if d else "") + "\n")
    if not b.links and not getattr(b, "can_links", []):
        w("  (none)\n")
    _print_warnings(session.events(kinds={"warn", "fault"}, chip="board"), w)


def _fmt_part_state(st: dict) -> str:
    out = []
    for k, v in st.items():
        if isinstance(v, dict):
            v = ",".join(f"{a}={b}" for a, b in v.items())
        elif isinstance(v, float):
            v = f"{v:.6g}"
        out.append(f"{k}={v}")
    return " ".join(out)


def print_parts_summary(session: Session, out=None) -> None:
    """Board parts: type, port -> net wiring, final state (LED on/off + changes, probe stats, counters)."""
    out = out or sys.stdout
    b = session.board
    if b is None or not b.parts:
        return
    w = out.write
    w(f"\nparts ({len(b.parts)}):\n")
    for p in b.parts.values():
        st = p.state()
        ports = " ".join(f"{k}={(n.name if (n := b.part_net(p.id, k)) is not None else '-')}" for k in p.ports)
        if p.type == "led":
            desc = f"{'ON ' if st['on'] else 'off'} ({st['changes']} changes, active {p.params['active']})"
        elif p.type == "probe":
            f = st.get("freq_hz")
            desc = (f"level {st['level']}, {st['rises']} rises / {st['falls']} falls"
                    + (f", {_fmt_hz(f)}" if f else "")
                    + (f", last change {_fmt_ns(st['last_change_ns'])}" if st.get("last_change_ns") is not None else ""))
        elif p.type == "counter":
            desc = f"count {st['count']}"
        else:
            desc = _fmt_part_state(st)
        w(f"  {p.id:<8} {p.type:<8} {ports:<34} {desc}\n")


class _NoChipTag:
    """Single-chip session that got a board for its parts: print chip events untagged, as without parts."""

    def __init__(self, sink):
        self.sink = sink
        self._unsub = None

    def __call__(self, ev) -> None:
        if ev.fields.get("chip") not in (None, "board"):
            from .log import LogEvent
            ev = LogEvent(ev.kind, ev.t_ns, {k: v for k, v in ev.fields.items() if k != "chip"}, ev.seq)
        self.sink(ev)

    def attach(self, log) -> "_NoChipTag":
        self._unsub = log.subscribe(self)
        return self

    def flush(self) -> None:
        self.sink.flush()

    def detach(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        self.sink.flush()


class TimeOrderedFeed:
    """Board runs: chips run in lockstep quanta, so their events reach the merged log slightly out of time order.
    Buffer them and release, in time order, those no chip can precede any more (t <= the board's earliest now)."""

    def __init__(self, sink, board):
        import heapq
        self._heapq = heapq
        self.sink = sink
        self.board = board
        self._heap: list = []
        self._unsub = None

    def attach(self, log) -> "TimeOrderedFeed":
        self._unsub = log.subscribe(self)
        return self

    def __call__(self, ev) -> None:
        self._heapq.heappush(self._heap, (ev.t_ns, ev.seq, ev))
        if len(self._heap) > 64 or ev.fields.get("chip") == "board":
            self.release(self.board.now_ns())

    def release(self, upto: float | None = None) -> None:
        h = self._heap
        while h and (upto is None or h[0][0] <= upto):
            self.sink(self._heapq.heappop(h)[2])

    def detach(self) -> None:
        self.release(None)
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        self.sink.detach()


def print_host_summary(session: Session, out=None) -> None:
    """Host serial endpoints and real-time pacing (only when there are any)."""
    out = out or sys.stdout
    st = session.host_status()
    eps, rt = st["endpoints"], st["realtime"]
    if not eps and not rt.get("enabled"):
        return
    out.write("\nhost serial:\n")
    for e in eps:
        out.write(f"  {e['name']:<10} {e.get('transport') or '-':<7} {e.get('path') or '-'}  line {e.get('line') or '-'}"
                  f"  in {e.get('bytes_in', 0)}  out {e.get('bytes_out', 0)}  lost {e.get('dropped', 0)} framing / "
                  f"{e.get('dropped_out', 0)} host closed\n")
    if rt.get("enabled"):
        ratio = f", achieved x{rt['ratio']:.3g}" if rt.get("ratio") else ""
        out.write(f"  real time x{rt['speed']:g}: lag {rt['lag_ms']:.1f} ms (max {rt['max_lag_ms']:.1f} ms), "
                  f"fell behind {rt['rebases']} time(s){ratio}, waited {rt['waited_s']:.2f} s\n")


def print_pin_report(session: Session, out=None) -> None:
    """--pin-report: chipem.pinreport.format_pin_report(chip) per chip (module owned by another component)."""
    out = out or sys.stdout
    try:
        from .pinreport import format_pin_report
    except ImportError:
        out.write("\n(pin report not available: chipem.pinreport is missing)\n")
        return
    multi = len(session.chips) > 1
    for cid, chip in session.chips.items():
        text = str(format_pin_report(chip) or "")
        if multi:
            out.write(f"\n=== pin report chip {cid} ===\n")
        else:
            out.write("\n")
        out.write(_prefixed(text if text.endswith("\n") else text + "\n", f"{cid}| " if multi else ""))


def main(args) -> int:
    try:
        return _main(args)
    except BrokenPipeError:              # e.g. `chipem run ... | head`
        import os
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        return 0


def _serve_gdb(session, args):
    """`chipem run --gdb PORT`: start halted, serve one debugger session on this (the emulation) thread until the
    client kills the target (`kill`) or detaches; the run summary follows."""
    from .gdbserver import start_for_session
    srv, ex = start_for_session(session, args.gdb, chip=getattr(args, "gdb_chip", None),
                                log=lambda m: print(m, file=sys.stderr, flush=True))
    print(f"gdb server listening on 127.0.0.1:{srv.port} (halted at reset; in gdb: target extended-remote "
          f":{srv.port}); detach or kill ends the run", file=sys.stderr, flush=True)
    try:
        ex.serve(lambda: srv.killed or (srv.clients > 0 and srv.conn is None))
    finally:
        srv.close()
    last = session.debug.last_stop or {}
    return getattr(StopReason, str(last.get("reason")), StopReason.USER) if last else StopReason.USER


def _main(args) -> int:
    dur = args.duration
    forever = dur is not None and str(dur).strip().lower() in ("forever", "inf", "until-ctrl-c")
    ns = parse_time(dur) if dur is not None and not forever else 100e6
    session = Session.from_args(args)
    from .hostserial import print_endpoints
    if session.host_endpoints(transport_only=True):
        print_endpoints(session)
        if dur is None:
            forever = True                   # interactive host bridge: run until Ctrl-C
    if forever:
        ns = float("inf")
    chips = session.chips
    multi = len(chips) > 1
    for chip in chips.values():            # (--max-slice and the other speed options: Session.from_args, perf.py)
        if args.stop_on_fault:
            chip.halt_on_fault = True
    quiet = args.quiet or args.summary_only
    printer = None
    if not quiet:
        if args.events:
            kinds = {k.strip() for k in args.events.split(",") if k.strip()}
            bad = kinds - set(EVENT_KINDS)
            if bad:
                raise ValueError(f"unknown event kind(s): {', '.join(sorted(bad))} (known: {', '.join(EVENT_KINDS)})")
        else:
            kinds = set(PinEventPrinter.DEFAULT_KINDS) | {"part"} | ({"mmio"} if args.log_mmio else set())
        printer = PinEventPrinter(sys.stdout, kinds, pin_width=8 if multi else 5,
                                  collapse_ns=None if getattr(args, "all_edges", False) else 10e6)
        if not multi and session.board is not None:
            printer = _NoChipTag(printer)
        # replay what happened during construction/reset (e.g. reset event, stimulus at t=0)
        for ev in session.events(kinds=kinds):
            if ev.kind == "warn" and "not available" in str(ev.fields.get("msg", "")):
                continue          # missing-model warnings are listed in the summary
            printer(ev)
        if multi:
            printer = TimeOrderedFeed(printer, session.board).attach(session.log)
        else:
            printer.attach(session.log)

    start = {cid: (c.clock.now_ns(), c.clock.cycles, getattr(c, "insns", 0)) for cid, c in chips.items()}
    wall0 = time.perf_counter()
    reason: StopReason | None = None
    try:
        try:
            if getattr(args, "gdb", None) is not None:
                reason = _serve_gdb(session, args)     # halted; gdb drives the run (chipem/gdbserver.py)
            else:
                reason = session.run_for(ns)
        except KeyboardInterrupt:
            print("\n(interrupted)", file=sys.stderr)
    finally:
        session.close()                  # flush/close the VCD even on errors or a broken pipe
    wall = time.perf_counter() - wall0
    if printer is not None:
        printer.detach()
    stopper = getattr(session.board, "last_stop_chip", None) if multi else None
    for cid in chips:
        t0, c0, i0 = start[cid]
        if multi:
            import io
            buf = io.StringIO()
            r, note = reason, ""
            if stopper is not None and cid != stopper:       # this chip was only halted with the board
                r, note = StopReason.USER, f" (board stopped by {stopper}: {reason.name})"
            print_summary(session, reason=r, wall_s=wall, t0_ns=t0, c0=c0, i0=i0, chip=cid, out=buf,
                          stop_note=note)
            sys.stdout.write(_prefixed(buf.getvalue(), f"{cid}| "))
        else:
            print_summary(session, reason=reason, wall_s=wall, t0_ns=t0, c0=c0, i0=i0)
    if multi:
        print_board_summary(session)
    print_parts_summary(session)
    print_host_summary(session)
    if getattr(args, "pin_report", False):
        print_pin_report(session)
    sys.stdout.flush()
    if reason is StopReason.FAULT or any(c.arch.lockup for c in chips.values()):
        return 3
    return 0
