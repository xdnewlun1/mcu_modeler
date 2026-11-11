"""80C51 external data bus as seen by MOVX (XDATA space, tagged 0x10000-0x1FFFF): P0 = multiplexed A0-A7 / D0-D7
latched by ALE, P2 = A8-A15 (MOVX @DPTR; MOVX @Ri leaves the P2 latch on P2), P3.6 WR / P3.7 RD strobes
(data sheet Table 3, sec. 6.2.1 Table 7: with AUXR.EXTRAM = 0 MOVX below 0300H stays on chip and asserts no
strobe).

A board `sram` device (chipem/devices/sram.py, `devices: [{type: sram, chip: A, bus: XBUS, size: 32K, width: 8}]`)
attaches with `attach_memory`: reads and writes of the bus reach it (location = address modulo its size: upper
address lines not connected).  Without a device the bus floats: reads return FFH (a pulled-up P0) and writes are
lost; one `info` the first time.  With --log-mmio every access is logged as an `mmio` event.

Bus signals are computed lazily: the core performs a MOVX in one step and the pins are not toggled per access
(that would cost two pin events per byte on the hot path).  Every access is recorded (`cycles`, a ring of
`params.history` entries, default 4096) and `waveform(t0_ns, t1_ns)` derives the ALE, RD (P3.7), WR (P3.6), P0
(address / data / float) and P2 (address high byte) edges of that window on demand.  Timing from data sheet
Table 55 (values in Tcy(clk) for 6-clock mode, scaled by clocks-per-machine-cycle / 6 in 12-clock mode): within
the 2-machine-cycle MOVX starting at T0 (unit u = clocks_per_mc / 6 oscillator periods) the address ALE pulse is
high T0+3u..T0+5u (tLHLL = 2u, P0 carries A0-A7 from T0+3u, tAVLL >= 1u), RD/WR is low T0+8u..T0+14u (tLLWL =
3u, tRLRH = tWLWH = 6u), the next ALE rises 1u after RD/WR (tWHLH); ALE otherwise pulses at fosc/6 (high 2u of
every 3u) and the pulses that would fall inside the access are skipped (sec. 7 "one ALE pulse is skipped during each
access to external data memory"; AUXR.AO = 1: ALE only during MOVX/MOVC).  P0 shows the data from WR - 6u (tQVWH)
to WR + 1u (tWHQX) on writes and is driven by the memory (data, else floating) while RD is low.  Derived from the
table, not measured on silicon; P0 between accesses and PSEN (code fetches from internal flash) are not shown.
"""
from __future__ import annotations

from collections import deque
from typing import Any

from ..base import MMIOBlock
from ..registry import register


@register("mcs51.xbus")
class XBus(MMIOBlock):
    model = "mcs51.xbus"

    def __init__(self, name: str, base: int, size: int, regs=(), *, params: dict | None = None, log: Any = None):
        super().__init__(name, base, size, (), ram_like=False, log=log, params=params)
        self.devices: list[Any] = []
        self.reads = self.writes = 0
        self._noted = False
        self.cycles: deque = deque(maxlen=int(self.params.get("history", 4096)))   # (t0_cycles, cpm, "r"/"w", addr, data)

    def attach_memory(self, dev: Any) -> None:
        self.devices.append(dev)

    def reset(self) -> None:
        super().reset()
        self.cycles.clear()

    def _record(self, kind: str, offset: int, value: int) -> None:
        chip = self.chip
        if chip is None:
            return
        cpm = getattr(chip.core, "clocks_per_mc", 12)
        # the access is seen at the end of the MOVX (the core charges its 2 machine cycles before executing it)
        self.cycles.append((chip.now_cycles() - 2 * cpm, cpm, kind, offset & 0xFFFF, value & 0xFF))

    def waveform(self, t0_ns: float = 0.0, t1_ns: float | None = None) -> dict[str, list[tuple[float, Any]]]:
        """Edges of ALE, RD, WR (levels "H"/"L"), P0 (byte value, or None = floating) and P2 (byte) between t0_ns and
        t1_ns, derived from the recorded MOVX accesses (see the module docstring)."""
        chip = self.chip
        clk = chip.clock
        ns = clk.cycles_to_ns
        if t1_ns is None:
            t1_ns = ns(chip.now_cycles())
        out: dict[str, list] = {"ALE": [], "RD": [], "WR": [], "P0": [], "P2": []}
        acc = [a for a in self.cycles if ns(a[0] + 2 * a[1] + 1 * a[1]) >= t0_ns and ns(a[0]) <= t1_ns]
        sc = None
        for b in chip.peripherals.values():
            if getattr(b, "model", "") == "mcs51.sysctrl":
                sc = b
        ao = bool(sc is not None and "AUXR" in sc.regs and sc.get("AUXR") & 1)
        skip: list[tuple[float, float]] = []
        for t0, cpm, kind, addr, data in acc:
            u = cpm / 6.0
            a = t0 + 3 * u
            strobe = "RD" if kind == "r" else "WR"
            out["ALE"] += [(ns(a), "H"), (ns(a + 2 * u), "L")]
            out["P0"].append((ns(a), addr & 0xFF))
            out["P2"].append((ns(a), addr >> 8))
            out[strobe] += [(ns(a + 5 * u), "L"), (ns(a + 11 * u), "H")]
            if kind == "w":
                out["P0"] += [(ns(a + 5 * u), data), (ns(a + 12 * u), None)]
            else:
                out["P0"] += [(ns(a + 5 * u), data if self.devices else None), (ns(a + 11 * u), None)]
            skip.append((a + 2 * u, a + 12 * u))
        if not ao:                                     # free-running ALE at fosc/6, minus the skipped pulses
            cpm = getattr(chip.core, "clocks_per_mc", 12)
            u = cpm / 6.0
            c0 = clk.cycles + (t0_ns - clk.now_ns()) * clk.hz / 1e9
            c1 = clk.cycles + (t1_ns - clk.now_ns()) * clk.hz / 1e9
            k = max(int(c0 // (3 * u)), 0)
            while k * 3 * u <= c1 and len(out["ALE"]) < 200000:
                r = k * 3 * u
                if not any(lo - 2 * u <= r < hi for lo, hi in skip):
                    out["ALE"] += [(ns(r), "H"), (ns(r + 2 * u), "L")]
                k += 1
        for key in out:
            out[key] = sorted((e for e in out[key] if t0_ns <= e[0] <= t1_ns), key=lambda e: e[0])
        return out

    def _float(self) -> None:
        if not self._noted:
            self._noted = True
            self.emit("info", block=self.name, msg=f"{self.name}: MOVX to the external bus with no memory attached "
                      "(reads FFH, writes are lost)")

    def read(self, offset: int, size: int) -> int:
        self.reads += 1
        v = 0
        for i in range(size):
            if self.devices:
                d = self.devices[0]
                b = d.read((offset + i) % d.size) & 0xFF
            else:
                self._float()
                b = 0xFF
            v |= b << (8 * i)
        self._record("r", offset, v)
        if self.log_all:
            self.emit("mmio", block=self.name, addr=self.base + offset, reg=None, size=size, access="r", value=v)
        return v

    def write(self, offset: int, size: int, value: int) -> None:
        self.writes += 1
        self._record("w", offset, value)
        if not self.devices:
            self._float()
        for i in range(size):
            for d in self.devices:
                d.write((offset + i) % d.size, (value >> (8 * i)) & 0xFF, 0xFF)
        if self.log_all:
            self.emit("mmio", block=self.name, addr=self.base + offset, reg=None, size=size, access="w", value=value)
