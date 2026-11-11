#!/usr/bin/env python3
"""Speed benchmark: virtual time per wall second (×real) and MIPS per chip for a fixed set of samples and boards,
under one or more speed/fidelity presets (chipem/perf.py). Not part of the test suite; run it before and after
changes to the cores, the Chip/Board loops or hot peripheral paths so regressions show.

    .venv/bin/python tools/bench.py                       # default + fast presets, every case
    .venv/bin/python tools/bench.py --preset default c2000 # only cases whose name contains "c2000"
    .venv/bin/python tools/bench.py --for 50ms --warm 20ms --preset fast --preset accurate
    .venv/bin/python tools/bench.py --web                 # also through `chipem serve` with a stream client

Each case is warmed up first (start-up code: PLL locks, clock setup) and then timed over --for of virtual time.
Columns: ×real (virtual / wall), MIPS of each chip (emulated instructions per wall second, skipped polling
iterations included), the bottleneck chip (largest share of the wall time) and, with poll skip on, the share of
instructions that were skipped instead of executed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if "--root" in sys.argv:                      # measure another checkout (e.g. an exported older version)
    ROOT = Path(sys.argv[sys.argv.index("--root") + 1]).resolve()
sys.path.insert(0, str(ROOT))

# (name, chip or board, firmware or None, --clock)
CASES = [
    ("c2000 asm blinky (delay loop)", "tms320f28232", "firmware/samples_c2000/build/blinky.c28img", None),
    ("c2000 C blinky (TI cl2000)", "tms320f28232", "firmware/samples_c2000_c/build/blinky.out", None),
    ("k22 busy loop 120 MHz", "mk22fn1m0vll12", "firmware/samples/build/blinky.elf", 120e6),
    ("stm32 busy loop 168 MHz", "stm32f405zg", "firmware/samples_stm32/build/blinky.elf", 168e6),
    ("stm32 HAL uart_chat", "stm32f405zg", "firmware/samples_stm32_hal/build/uart_chat_hal.elf", None),
    ("board c2000_stm32_uart", "boards/c2000_stm32_uart.yaml", None, None),
    ("board k22_c2000_stm32_can", "boards/k22_c2000_stm32_can.yaml", None, None),
    ("board pwm_capture_3way", "boards/pwm_capture_3way.yaml", None, None),
    ("board k22_i2c_eeprom", "boards/k22_i2c_eeprom.yaml", None, None),
    ("board k22_sdcard", "boards/k22_sdcard.yaml", None, None),
    ("board stm32_spi_pair", "boards/stm32_spi_pair.yaml", None, None),
]


def _session(target: str, fw: str | None, clock: float | None):
    from chipem.session import Session
    os.chdir(ROOT)
    if fw is None:
        s = Session.from_board(target, quiet=True)
        s.pacer = None
        return s
    return Session(target, fw, clock_hz=clock, quiet=True)


def run_case(case, preset: str, warm_ns: float, ns: float) -> dict:
    name, target, fw, clock = case
    if fw is not None and not (ROOT / fw).exists() or fw is None and not (ROOT / target).exists():
        return {"name": name, "skip": "not built"}
    try:
        s = _session(target, fw, clock)
    except Exception as e:                       # a board whose firmware is not built
        return {"name": name, "skip": f"{type(e).__name__}: {e}"[:60]}
    try:
        if hasattr(s, "set_perf"):
            s.set_perf(preset=preset)
        elif preset != "default":
            return {"name": name, "skip": "no presets in this version"}
        s.run_for(warm_ns)
        i0 = {c: ch.insns for c, ch in s.chips.items()}
        k0 = {c: getattr(ch, "poll_skipped", 0) for c, ch in s.chips.items()}
        cwall = lambda c: getattr(s.board, "chip_wall", {}).get(c, 0.0) if s.board is not None else 0.0  # noqa: E731
        w0 = {c: cwall(c) for c in s.chips}
        t0 = time.perf_counter()
        s.run_for(ns)
        dt = time.perf_counter() - t0
        mips = {c: (ch.insns - i0[c]) / dt / 1e6 for c, ch in s.chips.items()}
        skipped = sum(getattr(ch, "poll_skipped", 0) - k0[c] for c, ch in s.chips.items())
        total = sum(ch.insns - i0[c] for c, ch in s.chips.items()) or 1
        bott = None
        if s.board is not None and len(s.chips) > 1:
            cw = {c: cwall(c) - w0[c] for c in s.chips}
            bott = max(cw, key=cw.get) if any(cw.values()) else None
        return {"name": name, "ratio": ns / 1e9 / dt, "wall": dt, "mips": mips, "bottleneck": bott,
                "skipped": skipped / total}
    finally:
        s.close()


def web_case(case, preset: str, secs: float = 5.0) -> dict:
    """The same case through `chipem serve --port 0` with an SSE stream client attached."""
    name, target, fw, clock = case
    args = [str(ROOT / ".venv/bin/chipem"), "serve", "--port", "0", "--preset", preset]
    args += ["--board", target] if fw is None else [fw, "--chip", target] + (["--clock", str(clock)] if clock else [])
    p = subprocess.Popen(args, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True, cwd=ROOT)
    try:
        url = None
        t_end = time.time() + 60
        while time.time() < t_end and url is None:
            m = re.search(r"(http://[\d.]+:\d+)", p.stderr.readline())
            url = m.group(1) if m else None
        if url is None:
            return {"name": name, "skip": "server did not start"}
        threading.Thread(target=lambda: [None for _ in p.stderr], daemon=True).start()
        stop = threading.Event()
        got = [0]

        def reader():
            r = urllib.request.urlopen(url + "/api/stream", timeout=30)
            while not stop.is_set():
                b = r.read1(65536)
                if not b:
                    break
                got[0] += len(b)
        threading.Thread(target=reader, daemon=True).start()

        def post(path, body):
            req = urllib.request.Request(url + path, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            return json.load(urllib.request.urlopen(req, timeout=30))

        def state():
            return json.load(urllib.request.urlopen(url + "/api/state", timeout=30))
        post("/api/run", {})
        time.sleep(1.0)
        s0, w0 = state(), time.time()
        time.sleep(secs)
        s1, w1 = state(), time.time()
        post("/api/pause", {})
        stop.set()
        return {"name": name + " (web)", "ratio": (s1["time_ns"] - s0["time_ns"]) / 1e9 / (w1 - w0),
                "wall": w1 - w0, "mips": {c: v["mips"] for c, v in (s1.get("speed") or {}).get("chips", {}).items()},
                "bottleneck": (s1.get("speed") or {}).get("bottleneck"), "skipped": None,
                "stream_kib_s": got[0] / 1024 / (w1 - w0)}
    finally:
        p.terminate()
        p.wait(5)


def fmt_row(r: dict, preset: str) -> str:
    if "skip" in r:
        return f"{r['name']:34s} {preset:9s} skipped ({r['skip']})"
    mips = " ".join(f"{c}={m:.1f}" for c, m in r["mips"].items())
    sk = f"{r['skipped'] * 100:5.1f}%" if r.get("skipped") else "    -"
    return (f"{r['name']:34s} {preset:9s} {r['ratio']:9.4f} {r['wall']:7.2f} {sk:>7s}  {r['bottleneck'] or '-':4s} {mips}"
            + (f"  stream {r['stream_kib_s']:.0f} KiB/s" if r.get("stream_kib_s") is not None else ""))


def main(argv=None) -> int:
    from chipem.session import parse_time
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("filter", nargs="*", help="only cases whose name contains one of these words")
    ap.add_argument("--preset", action="append", default=None, choices=("accurate", "default", "fast"),
                    help="preset(s) to measure (default: default and fast)")
    ap.add_argument("--for", dest="duration", default="20ms", help="virtual time measured (default 20ms)")
    ap.add_argument("--warm", default="20ms", help="virtual time run first, not measured (default 20ms)")
    ap.add_argument("--web", action="store_true", help="also measure through chipem serve with a stream client")
    ap.add_argument("--root", default=None, help="checkout to measure (default: this one), e.g. an export of HEAD")
    a = ap.parse_args(argv)
    presets = a.preset or ["default", "fast"]
    ns, warm = parse_time(a.duration), parse_time(a.warm)
    cases = [c for c in CASES if not a.filter or any(f.lower() in c[0].lower() for f in a.filter)]
    print(f"{'case':34s} {'preset':9s} {'x real':>9s} {'wall s':>7s} {'skipped':>7s}  bott MIPS per chip")
    for case in cases:
        for p in presets:
            print(fmt_row(run_case(case, p, warm, ns), p), flush=True)
            if a.web:
                print(fmt_row(web_case(case, p), p), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
